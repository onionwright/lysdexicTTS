import sys
from kokoro import KPipeline
import soundfile as sf
import numpy as np

text = sys.argv[1] if len(sys.argv) > 1 else sys.stdin.read()
out = sys.argv[2] if len(sys.argv) > 2 else 'output.wav'

pipeline = KPipeline(lang_code='a', repo_id='hexgrad/Kokoro-82M')
audio = np.concatenate([a for _, _, a in pipeline(text, voice='af_heart')])
sf.write(out, audio, 24000)
