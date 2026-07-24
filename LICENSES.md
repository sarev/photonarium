# Licenses

## Photonarium

Copyright (c) 2024-2026, 7th software Ltd.

Licensed under the Apache License, Version 2.0 (the "License"); you may not
use this software except in compliance with the License. You may obtain a copy
of the License at:

https://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software distributed
under the License is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR
CONDITIONS OF ANY KIND, either express or implied. See the License for the
specific language governing permissions and limitations under the License.

---

## Third-Party Dependencies

Photonarium uses the following open-source libraries and models. All are
compatible with Photonarium's Apache 2.0 license.

### Core Framework

| Component | License | Link |
|-----------|---------|------|
| Python | PSF License | https://docs.python.org/3/license.html |
| Flask | BSD-3-Clause | https://github.com/pallets/flask/blob/main/LICENSE.txt |
| Waitress | ZPL-2.1 | https://github.com/Pylons/waitress/blob/main/LICENSE.txt |

### Machine Learning

| Component | License | Link |
|-----------|---------|------|
| PyTorch | BSD-3-Clause | https://github.com/pytorch/pytorch/blob/main/LICENSE |
| OpenCLIP | MIT | https://github.com/mlfoundations/open_clip/blob/main/LICENSE |
| HuggingFace Transformers | Apache 2.0 | https://github.com/huggingface/transformers/blob/main/LICENSE |
| timm (pytorch-image-models) | Apache 2.0 | https://github.com/huggingface/pytorch-image-models/blob/main/LICENSE |
| einops | MIT | https://github.com/arogozhnikov/einops/blob/main/LICENSE |
| facenet-pytorch | MIT | https://github.com/timesler/facenet-pytorch/blob/master/LICENSE.md |

### Pre-trained Models & Weights

| Model | License | Link |
|-------|---------|------|
| OpenAI CLIP (ViT-B-32) | MIT | https://github.com/openai/CLIP/blob/main/LICENSE |
| BLIP / BLIP-2 (Salesforce) | BSD-3-Clause | https://github.com/salesforce/LAVIS/blob/main/LICENSE.txt |
| LAION Aesthetic Predictor | MIT | https://github.com/LAION-AI/aesthetic-predictor/blob/main/LICENSE |
| NIMA (truskovskiyk) | MIT | https://github.com/truskovskiyk/nima.pytorch/blob/master/LICENSE |
| NAFNet (noise reduction + motion deblur) | MIT† | https://github.com/megvii-research/NAFNet/blob/main/LICENSE |
| Real-ESRGAN (super-resolution) | BSD-3-Clause | https://github.com/xinntao/Real-ESRGAN/blob/master/LICENSE |
| Restormer (defocus auto-sharpen) | MIT | https://github.com/swz30/Restormer/blob/main/LICENSE.md |
| VGGFace2 (facenet-pytorch) | CC BY-NC 4.0* | https://github.com/timesler/facenet-pytorch#pretrained-models |

*\* The VGGFace2 dataset (used to train InceptionResnetV1) has a non-commercial
license, but the facenet-pytorch model weights themselves are distributed under
MIT. For commercial use, consider retraining on a permissively-licensed dataset.*

*† NAFNet's code is MIT, but its pretrained weights are trained on the SIDD
(denoising) and GoPro (deblurring) datasets, which carry research/non-commercial
terms. This mirrors the VGGFace2 situation above: the vendored architecture is
freely redistributable; the weights' dataset provenance is the caveat for
commercial use.*

### Image Processing

| Component | License | Link |
|-----------|---------|------|
| Pillow | HPND | https://github.com/python-pillow/Pillow/blob/main/LICENSE |
| OpenCV | Apache 2.0 | https://github.com/opencv/opencv/blob/master/LICENSE |
| rawpy | MIT | https://github.com/letmaik/rawpy/blob/master/LICENSE |
| ImageHash | BSD-2-Clause | https://github.com/JohannesBuchner/imagehash/blob/master/LICENSE |
| ExifRead | BSD-3-Clause | https://github.com/ianare/exif-py/blob/develop/LICENSE.txt |

### Utilities

| Component | License | Link |
|-----------|---------|------|
| NumPy | BSD-3-Clause | https://github.com/numpy/numpy/blob/main/LICENSE.txt |
| orjson | Apache 2.0 / MIT | https://github.com/ijl/orjson/blob/master/LICENSE-APACHE |
| PyYAML | MIT | https://github.com/yaml/pyyaml/blob/main/LICENSE |
| Requests | Apache 2.0 | https://github.com/psf/requests/blob/main/LICENSE |

### Docker / GPU Acceleration (optional)

| Component | License | Link |
|-----------|---------|------|
| NVIDIA CUDA (via PyTorch) | NVIDIA EULA | https://docs.nvidia.com/cuda/eula/index.html |
| Intel Extension for PyTorch | Apache 2.0 | https://github.com/intel/intel-extension-for-pytorch/blob/master/LICENSE |

---

## Model Licensing Notes

When using alternative OpenCLIP or HuggingFace models, verify their licenses
individually. Some models (particularly those trained on restricted datasets)
may have non-commercial or research-only clauses.

The default model configuration (ViT-B-32/openai, BLIP-large) uses only
permissively-licensed weights suitable for commercial use.
