#!/usr/bin/env python3

# install torch with CUDA
# pip install open_clip_torch

import open_clip
import torch
from PIL import Image, ImageOps

device = 'cuda' if torch.cuda.is_available() else 'cpu'

model, _, preprocess = open_clip.create_model_and_transforms(
    'ViT-B-32',
    pretrained='laion2b_s34b_b79k',
)
model.eval().to(device)
tokenizer = open_clip.get_tokenizer('ViT-B-32')


def encode_image(path: str) -> torch.Tensor:
    img = ImageOps.exif_transpose(Image.open(path)).convert('RGB')
    x = preprocess(img).unsqueeze(0).to(device)
    with torch.inference_mode(), torch.cuda.amp.autocast(enabled=(device == 'cuda')):
        v = model.encode_image(x)
        v = v / v.norm(dim=-1, keepdim=True)
    return v  # shape: (1, D)


def encode_text(query: str) -> torch.Tensor:
    t = tokenizer([query]).to(device)
    with torch.inference_mode(), torch.cuda.amp.autocast(enabled=(device == 'cuda')):
        v = model.encode_text(t)
        v = v / v.norm(dim=-1, keepdim=True)
    return v  # shape: (1, D)


# Score is cosine similarity in [-1, 1]; sort descending for “best match first”.
v_img = encode_image('photo.jpg')
v_txt = encode_text('a cat sitting on a sofa')
score = float((v_txt @ v_img.T).item())
print(score)
