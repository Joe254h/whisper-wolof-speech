import os
os.environ["TQDM_DISABLE"] = "1"

import torch
from transformers import pipeline
from transformers.utils import logging

logging.disable_progress_bar()

MODEL_PATH = "./final-whisper-wolof"

device = 0 if torch.cuda.is_available() else -1

pipe = pipeline(
    "automatic-speech-recognition",
    model=MODEL_PATH,
    device=device
)

result = pipe(
    "./data/sample_200.wav"
)

print("\nTRANSCRIPTION :")
print(result["text"])
