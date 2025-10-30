from flask import Flask, request, jsonify, render_template_string, send_file
from openai import OpenAI
import os
import base64
import json
import uuid
from gtts import gTTS
import tempfile
import threading
import time
import io
import requests
from reportlab.pdfgen import canvas
from reportlab.lib.units import inch

app = Flask(__name__)

# Initialize OpenAI client with API key from environment variable
api_key = os.getenv('OPENAI_API_KEY', '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')
client = OpenAI(api_key=api_key) if api_key else None




def _ensure_openai_ready():
    if client is None:
        raise RuntimeError('OPENAI_API_KEY is not set on the server.')

# Directories and in-memory job tracking
GENERATED_BOOKS_DIR = os.path.join(tempfile.gettempdir(), 'flask_storybooks')
os.makedirs(GENERATED_BOOKS_DIR, exist_ok=True)
STORYBOOK_JOBS = {}

def _set_job(job_id, **kwargs):
    job = STORYBOOK_JOBS.get(job_id, {})
    job.update(kwargs)
    STORYBOOK_JOBS[job_id] = job
    return job

def _download_image_to(path, url):
    r = requests.get(url, timeout=60)
    r.raise_for_status()
    with open(path, 'wb') as f:
        f.write(r.content)

def _safe_json_parse(text):
    try:
        return json.loads(text)
    except Exception:
        return None

def _analyze_child_features(image_bytes):
    _ensure_openai_ready()
    base64_image = base64.b64encode(image_bytes).decode('utf-8')
    prompt = (
        "Analyze this child's face and return a concise JSON with keys: eye_color, hair_color, hair_style, skin_tone, age_guess, notable_features. "
        "Keep values short and specific."
    )
    resp = client.chat.completions.create(
        model="gpt-4o",
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}}
            ]
        }],
        max_tokens=300,
        temperature=0.2
    )
    content = resp.choices[0].message.content.strip()
    data = _safe_json_parse(content)
    if not data:
        # Fallback: wrap as plain text
        data = {"notes": content}
    return data

def _generate_story_json(story_key, gender, child_traits):
    _ensure_openai_ready()
    story_name = "Little Red Riding Hood" if story_key == 'lrrh' else "Jack and the Beanstalk"
    gender_text = 'boy' if gender == 'boy' else 'girl'
    trait_text = json.dumps(child_traits)
    prompt = f"""
Create a JSON structure for a 12-page children's book version of {story_name} featuring a {gender_text} as the main character.
For each page, provide:
1. Page number (1-12)
2. Scene description
3. Text for the page (2-3 sentences, age-appropriate)
4. Detailed image generation prompt in a consistent, friendly storybook illustration style.
Maintain character consistency throughout. The main character has traits: {trait_text}.
IMPORTANT: Ensure image prompts request a square 8.5 x 8.5 inch full-bleed composition, high quality, and include the page text embedded within the illustration (no separate overlay), legible, with no typos.
Return ONLY valid JSON with keys: story_title, pages[].
"""
    resp = client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=1800,
        temperature=0.6
    )
    text = resp.choices[0].message.content.strip()
    data = _safe_json_parse(text)
    if not data:
        raise RuntimeError("AI did not return valid JSON for story outline")
    if 'pages' not in data or not isinstance(data['pages'], list) or len(data['pages']) < 12:
        raise RuntimeError("Story JSON missing 12 pages")
    return data

def _generate_image_with_retry(prompt, size="1024x1024", retries=2):
    last_err = None
    for attempt in range(retries+1):
        try:
            _ensure_openai_ready()
            resp = client.images.generate(
                model="dall-e-3",
                prompt=prompt,
                size=size,
                quality="standard",
                n=1
            )
            return resp.data[0].url
        except Exception as e:
            last_err = e
            if attempt < retries:
                time.sleep(10 * (attempt + 1))
            else:
                raise last_err

def _compile_pdf(image_paths, out_pdf_path):
    # 8.5in x 8.5in full-bleed
    page_size = (8.5 * inch, 8.5 * inch)
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=page_size)
    for img in image_paths:
        # Draw edge-to-edge
        c.drawImage(img, 0, 0, width=page_size[0], height=page_size[1])
        c.showPage()
    c.save()
    with open(out_pdf_path, 'wb') as f:
        f.write(buf.getvalue())

def _run_storybook_job(job_id, image_bytes, story_key, gender):
    try:
        _set_job(job_id, state='working', progress=1, message='Analyzing child features...')
        traits = _analyze_child_features(image_bytes)

        _set_job(job_id, progress=5, message='Generating 12-page story outline...')
        story_json = _generate_story_json(story_key, gender, traits)

        # Prepare dirs
        job_dir = os.path.join(GENERATED_BOOKS_DIR, job_id)
        os.makedirs(job_dir, exist_ok=True)

        image_paths = []
        total_pages = 12
        for idx, page in enumerate(story_json.get('pages', [])[:12], start=1):
            _set_job(job_id, progress=min(5 + int(80 * (idx-1)/total_pages), 90), message=f"Creating page {idx} of 12...")
            base_prompt = page.get('image_prompt') or ''
            # Reinforce consistency and formatting
            consistency = (
                "Consistent main character across pages; same eye color, hair color, hairstyle, clothing elements as previously described. "
                "Square composition, full-bleed to the edges, 8.5x8.5 inch feel, high resolution. Embed the page text clearly and without typos."
            )
            full_prompt = f"{base_prompt}\n\n{consistency}"
            url = _generate_image_with_retry(full_prompt, size="1024x1024")
            img_path = os.path.join(job_dir, f"page_{idx:02d}.png")
            _download_image_to(img_path, url)
            image_paths.append(img_path)

        _set_job(job_id, progress=92, message='Compiling PDF...')
        pdf_path = os.path.join(job_dir, f"storybook_{job_id}.pdf")
        _compile_pdf(image_paths, pdf_path)

        _set_job(job_id, state='done', progress=100, message='Completed! Your book is ready.', pdf_path=pdf_path, download_url=f"/storybook/download/{job_id}.pdf")
    except Exception as e:
        _set_job(job_id, state='error', progress=0, message=f"Error: {str(e)}")

# HTML template for the comprehensive AI web application
HTML_TEMPLATE = '''
<!DOCTYPE html>
<html>
<head>
    <title>AI Capabilities Demo</title>
    <style>
        body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; margin: 0; padding: 20px; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); min-height: 100vh; }
        .container { max-width: 1200px; margin: 0 auto; }
        .header { text-align: center; color: white; margin-bottom: 30px; }
        .header h1 { font-size: 2.5em; margin-bottom: 10px; text-shadow: 2px 2px 4px rgba(0,0,0,0.3); }
        .header p { font-size: 1.2em; opacity: 0.9; }
        .features-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(350px, 1fr)); gap: 20px; margin-bottom: 30px; }
        .feature-card { background: white; padding: 25px; border-radius: 15px; box-shadow: 0 8px 25px rgba(0,0,0,0.1); transition: transform 0.3s ease; }
        .feature-card:hover { transform: translateY(-5px); }
        .feature-card h3 { color: #333; margin-bottom: 15px; font-size: 1.3em; }
        .form-group { margin: 15px 0; }
        label { display: block; margin-bottom: 8px; font-weight: 600; color: #555; }
        input, textarea, select { width: 100%; padding: 12px; border: 2px solid #e1e5e9; border-radius: 8px; font-size: 14px; transition: border-color 0.3s ease; }
        input:focus, textarea:focus, select:focus { outline: none; border-color: #667eea; }
        button { background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; padding: 12px 25px; border: none; border-radius: 8px; cursor: pointer; font-size: 14px; font-weight: 600; transition: all 0.3s ease; }
        button:hover { transform: translateY(-2px); box-shadow: 0 5px 15px rgba(0,0,0,0.2); }
        .result { margin-top: 20px; padding: 20px; background-color: #f8f9fa; border-radius: 8px; border-left: 4px solid #28a745; }
        .loading { display: none; color: #666; text-align: center; padding: 20px; }
        .image-result { text-align: center; margin-top: 15px; }
        .image-result img { max-width: 100%; border-radius: 8px; box-shadow: 0 4px 15px rgba(0,0,0,0.1); }
        .json-result { background: #2d3748; color: #e2e8f0; padding: 15px; border-radius: 8px; font-family: 'Courier New', monospace; white-space: pre-wrap; }
        .audio-controls { margin-top: 15px; }
        .file-input { margin: 10px 0; }
        .error { background-color: #f8d7da; color: #721c24; padding: 15px; border-radius: 8px; border-left: 4px solid #dc3545; }
        .success { background-color: #d4edda; color: #155724; padding: 15px; border-radius: 8px; border-left: 4px solid #28a745; }
        .progress-wrapper { background: #eee; border-radius: 8px; overflow: hidden; height: 12px; }
        .progress-bar { height: 12px; width: 0%; background: linear-gradient(90deg, #4ade80, #22c55e); transition: width 0.3s ease; }
        .muted { color: #6b7280; font-size: 0.9em; }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>🤖 AI Capabilities Demo</h1>
            <p>Comprehensive OpenAI API Integration with Flask</p>
        </div>

        <div class="features-grid">
            <!-- Personalized Fairy Tale Generator -->
            <div class="feature-card">
                <h3>📚 Personalized Fairy Tale Generator (v0.1)</h3>
                <form id="storybookForm">
                    <div class="form-group">
                        <label for="childImage">Upload child photo (face):</label>
                        <input type="file" id="childImage" accept="image/*" class="file-input" required>
                    </div>
                    <div class="form-group">
                        <label for="storySelect">Select story:</label>
                        <select id="storySelect" required>
                            <option value="lrrh">Little Red Riding Hood</option>
                            <option value="jack">Jack and the Beanstalk</option>
                        </select>
                    </div>
                    <div class="form-group">
                        <label>Gender:</label>
                        <div>
                            <label><input type="radio" name="gender" value="boy" checked> Boy</label>
                            <label style="margin-left:16px;"><input type="radio" name="gender" value="girl"> Girl</label>
                        </div>
                    </div>
                    <button type="submit">Generate Story</button>
                    <div class="loading" id="storybookLoading">Starting generation...</div>
                </form>
                <div id="storybookProgress" class="result" style="display:none;">
                    <div class="form-group">
                        <div class="progress-wrapper"><div class="progress-bar" id="progressBar"></div></div>
                        <div class="muted" id="progressMsg" style="margin-top:8px;">Preparing...</div>
                    </div>
                    <div id="downloadBlock" style="display:none; margin-top:10px;">
                        <a id="downloadLink" href="#" download>Download Your Book (PDF)</a>
                    </div>
                </div>
            </div>
            <!-- Text Generation -->
            <div class="feature-card">
                <h3>📝 Text Generation</h3>
                <form id="textForm">
                    <div class="form-group">
                        <label for="textPrompt">Enter your prompt:</label>
                        <textarea id="textPrompt" rows="3" placeholder="Generate a 200-word story about a unicorn...">Generate a 200-word story about a unicorn</textarea>
                    </div>
                    <button type="submit">Generate Text</button>
                    <div class="loading" id="textLoading">Generating text... ✨</div>
                </form>
                <div id="textResult" class="result" style="display: none;">
                    <h4>Generated Text:</h4>
                    <p id="textOutput"></p>
                </div>
            </div>

            <!-- Image Generation -->
            <div class="feature-card">
                <h3>🎨 Image Generation</h3>
                <form id="imageForm">
                    <div class="form-group">
                        <label for="imagePrompt">Describe the image:</label>
                        <textarea id="imagePrompt" rows="3" placeholder="A majestic unicorn in a magical forest...">A majestic unicorn in a magical forest with rainbow colors</textarea>
                    </div>
                    <button type="submit">Generate Image</button>
                    <div class="loading" id="imageLoading">Creating image... 🎨</div>
                </form>
                <div id="imageResult" class="result" style="display: none;">
                    <h4>Generated Image:</h4>
                    <div class="image-result">
                        <img id="generatedImage" alt="Generated image">
                    </div>
                </div>
            </div>

            <!-- Structured Data (JSON) -->
            <div class="feature-card">
                <h3>📊 Structured Data (JSON)</h3>
                <form id="jsonForm">
                    <div class="form-group">
                        <label for="jsonPrompt">Generate structured data about:</label>
                        <input type="text" id="jsonPrompt" placeholder="product descriptions, user profiles, etc." value="product descriptions for a tech startup">
                    </div>
                    <button type="submit">Generate JSON</button>
                    <div class="loading" id="jsonLoading">Generating structured data... 📊</div>
                </form>
                <div id="jsonResult" class="result" style="display: none;">
                    <h4>Structured Data:</h4>
                    <div class="json-result" id="jsonOutput"></div>
                </div>
            </div>

            <!-- Vision Capabilities -->
            <div class="feature-card">
                <h3>👁 Vision Analysis</h3>
                <form id="visionForm">
                    <div class="form-group">
                        <label for="imageFile">Upload an image:</label>
                        <input type="file" id="imageFile" accept="image/*" class="file-input">
                    </div>
                    <div class="form-group">
                        <label for="visionPrompt">Analysis prompt:</label>
                        <textarea id="visionPrompt" rows="2" placeholder="Describe what you see in detail...">Describe what you see in this image in detail</textarea>
                    </div>
                    <button type="submit">Analyze Image</button>
                    <div class="loading" id="visionLoading">Analyzing image... 👁</div>
                </form>
                <div id="visionResult" class="result" style="display: none;">
                    <h4>Image Analysis:</h4>
                    <p id="visionOutput"></p>
                </div>
            </div>

            <!-- Audio Processing -->
            <div class="feature-card">
                <h3>🎤 Audio Processing</h3>
                <form id="audioForm">
                    <div class="form-group">
                        <label for="audioFile">Upload audio file:</label>
                        <input type="file" id="audioFile" accept="audio/*" class="file-input">
                    </div>
                    <div class="form-group">
                        <label for="audioText">Text to convert to speech:</label>
                        <textarea id="audioText" rows="2" placeholder="Enter text to convert to speech...">Hello! This is a test of text-to-speech conversion.</textarea>
                    </div>
                    <button type="submit">Process Audio</button>
                    <div class="loading" id="audioLoading">Processing audio... 🎤</div>
                </form>
                <div id="audioResult" class="result" style="display: none;">
                    <h4>Audio Processing Results:</h4>
                    <p id="audioOutput"></p>
                    <div class="audio-controls">
                        <audio id="audioPlayer" controls style="width: 100%;"></audio>
                    </div>
                </div>
            </div>

            <!-- Translation Feature -->
            <div class="feature-card">
                <h3>🌍 Language Translation</h3>
                <form id="translationForm">
                    <div class="form-group">
                        <label for="translationText">Text to translate:</label>
                        <textarea id="translationText" rows="3" placeholder="Enter text to translate...">Hello, how are you today?</textarea>
                    </div>
                    <div class="form-group">
                        <label for="targetLanguage">Target language:</label>
                        <select id="targetLanguage">
                            <option value="spanish">Spanish</option>
                            <option value="french">French</option>
                            <option value="german">German</option>
                            <option value="italian">Italian</option>
                            <option value="portuguese">Portuguese</option>
                            <option value="chinese">Chinese</option>
                            <option value="japanese">Japanese</option>
                        </select>
                    </div>
                    <button type="submit">Translate</button>
                    <div class="loading" id="translationLoading">Translating... 🌍</div>
                </form>
                <div id="translationResult" class="result" style="display: none;">
                    <h4>Translation:</h4>
                    <p id="translationOutput"></p>
                </div>
            </div>
        </div>
    </div>

    <script>
        // Storybook Generation
        (function(){
            const form = document.getElementById('storybookForm');
            if (!form) return;
            const loading = document.getElementById('storybookLoading');
            const progressBox = document.getElementById('storybookProgress');
            const progressBar = document.getElementById('progressBar');
            const progressMsg = document.getElementById('progressMsg');
            const downloadBlock = document.getElementById('downloadBlock');
            const downloadLink = document.getElementById('downloadLink');
            let pollTimer = null;

            function setProgress(pct, msg){
                progressBar.style.width = (pct||0) + '%';
                progressMsg.textContent = msg || '';
            }

            function stopPolling(){ if (pollTimer) { clearInterval(pollTimer); pollTimer = null; } }

            async function poll(jobId){
                try {
                    const res = await fetch(`/storybook/status?job_id=${encodeURIComponent(jobId)}`);
                    const data = await res.json();
                    if (!data.success) { setProgress(0, 'Error: ' + (data.error||'Unknown')); stopPolling(); return; }
                    setProgress(data.progress||0, data.message||'');
                    if (data.state === 'done') {
                        stopPolling();
                        downloadLink.href = data.download_url;
                        downloadBlock.style.display = 'block';
                        return;
                    }
                    if (data.state === 'error') { stopPolling(); return; }
                } catch (e) {
                    setProgress(0, 'Error: ' + e.message);
                    stopPolling();
                }
            }

            form.addEventListener('submit', async function(e){
                e.preventDefault();
                const file = document.getElementById('childImage').files[0];
                if (!file) { alert('Please upload a child photo.'); return; }
                const story = document.getElementById('storySelect').value;
                const gender = (new FormData(form)).get('gender');
                loading.style.display = 'block';
                progressBox.style.display = 'none';
                downloadBlock.style.display = 'none';
                try {
                    const fd = new FormData();
                    fd.append('image', file);
                    fd.append('story', story);
                    fd.append('gender', gender);
                    const res = await fetch('/storybook/start', { method: 'POST', body: fd });
                    const data = await res.json();
                    if (data.success) {
                        loading.style.display = 'none';
                        progressBox.style.display = 'block';
                        setProgress(1, 'Generating story outline...');
                        stopPolling();
                        pollTimer = setInterval(() => poll(data.job_id), 2000);
                    } else {
                        loading.style.display = 'none';
                        alert('Error: ' + (data.error||'unknown'));
                    }
                } catch (err) {
                    loading.style.display = 'none';
                    alert('Error: ' + err.message);
                }
            });
        })();

        // Text Generation
        document.getElementById('textForm').addEventListener('submit', async function(e) {
            e.preventDefault();
            const prompt = document.getElementById('textPrompt').value;
            const loading = document.getElementById('textLoading');
            const result = document.getElementById('textResult');
            const output = document.getElementById('textOutput');

            loading.style.display = 'block';
            result.style.display = 'none';

            try {
                const response = await fetch('/generate-text', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ prompt: prompt })
                });
                const data = await response.json();
                if (data.success) {
                    output.textContent = data.text;
                    result.style.display = 'block';
                } else {
                    output.innerHTML = '<div class="error">Error: ' + data.error + '</div>';
                    result.style.display = 'block';
                }
            } catch (error) {
                output.innerHTML = '<div class="error">Error: ' + error.message + '</div>';
                result.style.display = 'block';
            } finally {
                loading.style.display = 'none';
            }
        });

        // Image Generation
        document.getElementById('imageForm').addEventListener('submit', async function(e) {
            e.preventDefault();
            const prompt = document.getElementById('imagePrompt').value;
            const loading = document.getElementById('imageLoading');
            const result = document.getElementById('imageResult');
            const image = document.getElementById('generatedImage');

            loading.style.display = 'block';
            result.style.display = 'none';

            try {
                const response = await fetch('/generate-image', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ prompt: prompt })
                });
                const data = await response.json();
                if (data.success) {
                    image.src = data.image_url;
                    result.style.display = 'block';
                } else {
                    image.alt = 'Error: ' + data.error;
                    result.style.display = 'block';
                }
            } catch (error) {
                image.alt = 'Error: ' + error.message;
                result.style.display = 'block';
            } finally {
                loading.style.display = 'none';
            }
        });

        // JSON Generation
        document.getElementById('jsonForm').addEventListener('submit', async function(e) {
            e.preventDefault();
            const prompt = document.getElementById('jsonPrompt').value;
            const loading = document.getElementById('jsonLoading');
            const result = document.getElementById('jsonResult');
            const output = document.getElementById('jsonOutput');

            loading.style.display = 'block';
            result.style.display = 'none';

            try {
                const response = await fetch('/generate-json', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ prompt: prompt })
                });
                const data = await response.json();
                if (data.success) {
                    output.textContent = JSON.stringify(data.json_data, null, 2);
                    result.style.display = 'block';
                } else {
                    output.textContent = 'Error: ' + data.error;
                    result.style.display = 'block';
                }
            } catch (error) {
                output.textContent = 'Error: ' + error.message;
                result.style.display = 'block';
            } finally {
                loading.style.display = 'none';
            }
        });

        // Vision Analysis
        document.getElementById('visionForm').addEventListener('submit', async function(e) {
            e.preventDefault();
            const fileInput = document.getElementById('imageFile');
            const prompt = document.getElementById('visionPrompt').value;
            const loading = document.getElementById('visionLoading');
            const result = document.getElementById('visionResult');
            const output = document.getElementById('visionOutput');

            if (!fileInput.files[0]) {
                output.innerHTML = '<div class="error">Please select an image file.</div>';
                result.style.display = 'block';
                return;
            }

            loading.style.display = 'block';
            result.style.display = 'none';

            const formData = new FormData();
            formData.append('image', fileInput.files[0]);
            formData.append('prompt', prompt);

            try {
                const response = await fetch('/analyze-image', {
                    method: 'POST',
                    body: formData
                });
                const data = await response.json();
                if (data.success) {
                    output.textContent = data.analysis;
                    result.style.display = 'block';
                } else {
                    output.innerHTML = '<div class="error">Error: ' + data.error + '</div>';
                    result.style.display = 'block';
                }
            } catch (error) {
                output.innerHTML = '<div class="error">Error: ' + error.message + '</div>';
                result.style.display = 'block';
            } finally {
                loading.style.display = 'none';
            }
        });

        // Audio Processing
        document.getElementById('audioForm').addEventListener('submit', async function(e) {
            e.preventDefault();
            const fileInput = document.getElementById('audioFile');
            const text = document.getElementById('audioText').value;
            const loading = document.getElementById('audioLoading');
            const result = document.getElementById('audioResult');
            const output = document.getElementById('audioOutput');
            const audioPlayer = document.getElementById('audioPlayer');

            loading.style.display = 'block';
            result.style.display = 'none';

            try {
                let response;
                if (fileInput.files[0]) {
                    // Speech to text
                    const formData = new FormData();
                    formData.append('audio', fileInput.files[0]);
                    response = await fetch('/speech-to-text', {
                        method: 'POST',
                        body: formData
                    });
                } else {
                    // Text to speech
                    response = await fetch('/text-to-speech', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ text: text })
                    });
                }

                const data = await response.json();
                if (data.success) {
                    if (data.transcription) {
                        output.textContent = 'Transcription: ' + data.transcription;
                    } else if (data.audio_url) {
                        output.textContent = 'Audio generated successfully!';
                        audioPlayer.src = data.audio_url;
                    }
                    result.style.display = 'block';
                } else {
                    output.innerHTML = '<div class="error">Error: ' + data.error + '</div>';
                    result.style.display = 'block';
                }
            } catch (error) {
                output.innerHTML = '<div class="error">Error: ' + error.message + '</div>';
                result.style.display = 'block';
            } finally {
                loading.style.display = 'none';
            }
        });

        // Translation
        document.getElementById('translationForm').addEventListener('submit', async function(e) {
            e.preventDefault();
            const text = document.getElementById('translationText').value;
            const language = document.getElementById('targetLanguage').value;
            const loading = document.getElementById('translationLoading');
            const result = document.getElementById('translationResult');
            const output = document.getElementById('translationOutput');

            loading.style.display = 'block';
            result.style.display = 'none';

            try {
                const response = await fetch('/translate', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ text: text, language: language })
                });
                const data = await response.json();
                if (data.success) {
                    output.textContent = data.translation;
                    result.style.display = 'block';
                } else {
                    output.innerHTML = '<div class="error">Error: ' + data.error + '</div>';
                    result.style.display = 'block';
                }
            } catch (error) {
                output.innerHTML = '<div class="error">Error: ' + error.message + '</div>';
                result.style.display = 'block';
            } finally {
                loading.style.display = 'none';
            }
        });
    </script>
</body>
</html>
'''


@app.route('/')
def home():
    """Serve the main page with the comprehensive AI demo"""
    return render_template_string(HTML_TEMPLATE)


# Storybook endpoints
@app.route('/storybook/start', methods=['POST'])
def storybook_start():
    try:
        if 'image' not in request.files:
            return jsonify({'success': False, 'error': 'No image file provided'}), 400
        image_file = request.files['image']
        story = request.form.get('story', 'lrrh')
        gender = request.form.get('gender', 'boy')
        if image_file.filename == '':
            return jsonify({'success': False, 'error': 'No image file selected'}), 400
        image_bytes = image_file.read()
        job_id = uuid.uuid4().hex
        _set_job(job_id, state='queued', progress=0, message='Queued...')
        t = threading.Thread(target=_run_storybook_job, args=(job_id, image_bytes, story, gender), daemon=True)
        t.start()
        return jsonify({'success': True, 'job_id': job_id})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/storybook/status', methods=['GET'])
def storybook_status():
    job_id = request.args.get('job_id')
    if not job_id or job_id not in STORYBOOK_JOBS:
        return jsonify({'success': False, 'error': 'Invalid job id'}), 400
    job = STORYBOOK_JOBS[job_id]
    resp = {
        'success': True,
        'state': job.get('state', 'working'),
        'progress': job.get('progress', 0),
        'message': job.get('message', ''),
    }
    if job.get('state') == 'done':
        resp['download_url'] = job.get('download_url')
    return jsonify(resp)


@app.route('/storybook/download/<job_id>.pdf', methods=['GET'])
def storybook_download(job_id):
    job = STORYBOOK_JOBS.get(job_id)
    if not job or job.get('state') != 'done':
        return jsonify({'success': False, 'error': 'Not ready'}), 400
    pdf_path = job.get('pdf_path')
    if not pdf_path or not os.path.isfile(pdf_path):
        return jsonify({'success': False, 'error': 'File not found'}), 404
    return send_file(pdf_path, as_attachment=True, download_name=f'storybook_{job_id}.pdf')


# Text Generation
@app.route('/generate-text', methods=['POST'])
def generate_text():
    """Generate text content using OpenAI"""
    try:
        data = request.get_json()
        prompt = data.get('prompt', 'Generate a 200-word story about a unicorn')

        _ensure_openai_ready()
        response = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=500,
            temperature=0.8
        )

        text = response.choices[0].message.content.strip()
        return jsonify({'success': True, 'text': text})

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# Image Generation
@app.route('/generate-image', methods=['POST'])
def generate_image():
    """Generate images using DALL-E"""
    try:
        data = request.get_json()
        prompt = data.get('prompt', 'A majestic unicorn in a magical forest')

        _ensure_openai_ready()
        response = client.images.generate(
            model="dall-e-3",
            prompt=prompt,
            size="1024x1024",
            quality="standard",
            n=1
        )

        image_url = response.data[0].url
        return jsonify({'success': True, 'image_url': image_url})

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# Structured Data (JSON)
@app.route('/generate-json', methods=['POST'])
def generate_json():
    """Generate structured JSON data"""
    try:
        data = request.get_json()
        prompt = data.get('prompt', 'product descriptions for a tech startup')

        json_prompt = f"""Generate structured JSON data for: {prompt}
        
        Return a JSON object with the following structure:
        {{
            "items": [
                {{
                    "name": "string",
                    "description": "string",
                    "price": "string",
                    "category": "string",
                    "features": ["string1", "string2", "string3"]
                }}
            ]
        }}
        
        Generate 3-5 items with realistic data."""

        _ensure_openai_ready()
        response = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[{"role": "user", "content": json_prompt}],
            max_tokens=800,
            temperature=0.7
        )

        json_text = response.choices[0].message.content.strip()
        # Try to parse and return as actual JSON
        try:
            json_data = json.loads(json_text)
            return jsonify({'success': True, 'json_data': json_data})
        except:
            return jsonify({'success': True, 'json_data': json_text})

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# Vision Analysis
@app.route('/analyze-image', methods=['POST'])
def analyze_image():
    """Analyze uploaded images using GPT-4 Vision"""
    try:
        if 'image' not in request.files:
            return jsonify({'success': False, 'error': 'No image file provided'}), 400

        file = request.files['image']
        prompt = request.form.get('prompt', 'Describe what you see in this image in detail')

        if file.filename == '':
            return jsonify({'success': False, 'error': 'No image file selected'}), 400

        # Read and encode the image
        image_data = file.read()
        base64_image = base64.b64encode(image_data).decode('utf-8')

        _ensure_openai_ready()
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{base64_image}"
                            }
                        }
                    ]
                }
            ],
            max_tokens=500
        )

        analysis = response.choices[0].message.content.strip()
        return jsonify({'success': True, 'analysis': analysis})

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# Speech to Text
@app.route('/speech-to-text', methods=['POST'])
def speech_to_text():
    """Convert speech to text using OpenAI Whisper"""
    try:
        if 'audio' not in request.files:
            return jsonify({'success': False, 'error': 'No audio file provided'}), 400

        file = request.files['audio']
        if file.filename == '':
            return jsonify({'success': False, 'error': 'No audio file selected'}), 400

        # Save temporary file
        with tempfile.NamedTemporaryFile(delete=False, suffix='.wav') as tmp_file:
            file.save(tmp_file.name)
            
            with open(tmp_file.name, 'rb') as audio_file:
                transcript = client.audio.transcriptions.create(
                    model="whisper-1",
                    file=audio_file
                )

        os.unlink(tmp_file.name)
        return jsonify({'success': True, 'transcription': transcript.text})

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


"""Text-to-Speech flow adjusted for frontend expectations.
POST /text-to-speech returns JSON with an audio_url, and the actual audio is
served by GET /audio/<filename>. This avoids streaming files in JSON responses.
"""

GENERATED_AUDIO_DIR = os.path.join(tempfile.gettempdir(), 'flask_generated_audio')
os.makedirs(GENERATED_AUDIO_DIR, exist_ok=True)

# Text to Speech (create)
@app.route('/text-to-speech', methods=['POST'])
def text_to_speech():
    try:
        data = request.get_json()
        text = data.get('text', 'Hello! This is a test of text-to-speech conversion.')

        tts = gTTS(text=text, lang='en', slow=False)
        filename = f"speech_{uuid.uuid4().hex}.mp3"
        file_path = os.path.join(GENERATED_AUDIO_DIR, filename)
        tts.save(file_path)

        return jsonify({'success': True, 'audio_url': f"/audio/{filename}"})

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

# Text to Speech (serve)
@app.route('/audio/<path:filename>', methods=['GET'])
def serve_audio(filename):
    try:
        file_path = os.path.join(GENERATED_AUDIO_DIR, filename)
        if not os.path.isfile(file_path):
            return jsonify({'success': False, 'error': 'File not found'}), 404
        return send_file(file_path, as_attachment=False, download_name=filename)
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# Translation
@app.route('/translate', methods=['POST'])
def translate():
    """Translate text using OpenAI"""
    try:
        data = request.get_json()
        text = data.get('text', 'Hello, how are you today?')
        language = data.get('language', 'spanish')

        language_map = {
            'spanish': 'Spanish',
            'french': 'French',
            'german': 'German',
            'italian': 'Italian',
            'portuguese': 'Portuguese',
            'chinese': 'Chinese',
            'japanese': 'Japanese'
        }

        target_lang = language_map.get(language, 'Spanish')
        prompt = f"Translate the following text to {target_lang}: {text}"

        response = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=200,
            temperature=0.3
        )

        translation = response.choices[0].message.content.strip()
        return jsonify({'success': True, 'translation': translation})

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


if __name__ == '__main__':
    print("🚀 Starting AI Capabilities Demo Server...")
    print("🌐 Web interface: http://localhost:5000")
    print("📝 Features available:")
    print("   • Text Generation")
    print("   • Image Generation (DALL-E)")
    print("   • Structured Data (JSON)")
    print("   • Vision Analysis")
    print("   • Speech-to-Text & Text-to-Speech")
    print("   • Language Translation")
    print("\n🎯 Ready for demo!")

    # Get port from environment variable (for Render deployment)
    port = int(os.environ.get('PORT', 5000))
    
    # Run the Flask app
    app.run(debug=False, host='0.0.0.0', port=port)
