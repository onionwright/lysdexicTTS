import gradio as gr
import numpy as np
from kokoro import KPipeline

pipeline = KPipeline(lang_code='a', repo_id='hexgrad/Kokoro-82M')

VOICES = ["af_heart", "af_bella", "af_nicole", "am_michael", "am_fenrir",
          "bf_emma", "bm_george"]

def speak(text, voice):
    if not text.strip():
        return None
    audio = np.concatenate([a for _, _, a in pipeline(text, voice=voice)])
    return (24000, np.asarray(audio))

gr.Interface(
    fn=speak,
    inputs=[gr.Textbox(lines=10, label="Text"),
            gr.Dropdown(VOICES, value="af_heart", label="Voice")],
    outputs=gr.Audio(label="Output"),
    title="Kokoro TTS",
).launch(inbrowser=True)
