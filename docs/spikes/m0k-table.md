| clip | t | variant | ms | vs real PSNR | vs real SSIM | vs fp32 PSNR | vs fp32 SSIM | max diff |
|---|---|---|---|---|---|---|---|---|
| | | conv16-trt | error: [ONNXRuntimeError] : 1 : FAIL : Load model from ~/.cache/buttereye-dev/p2/trt/rife_v4.26.fp16-keepio.onnx failed:Type Error: Type (tensor(float16)) of output arg (/Cast_2_output_0) of node (/Cast_2) does not match expected type (tensor(float)). | | | | | |
| | | conv16-cuda | error: [ONNXRuntimeError] : 1 : FAIL : Load model from ~/.cache/buttereye-dev/p2/trt/rife_v4.26.fp16-keepio.onnx failed:Type Error: Type (tensor(float16)) of output arg (/Cast_2_output_0) of node (/Cast_2) does not match expected type (tensor(float)). | | | | | |
| film-1920x804  | 600 | blend |  | 26.82 | 0.80769 |  |  |  |
| film-1920x804  | 600 | cuda32 | 24.4 | 33.65 | 0.93253 |  |  |  |
| film-1920x804  | 600 | trt32 | 17.5 | 33.65 | 0.93253 | 93.01 | 1.0 | 0.0019 |
| film-1920x804  | 600 | trt16 | 13.2 | 33.26 | 0.92679 | 46.88 | 0.99522 | 0.0915 |
| film-1920x804  | 1800 | blend |  | 42.29 | 0.9722 |  |  |  |
| film-1920x804  | 1800 | cuda32 | 19.7 | 45.44 | 0.98249 |  |  |  |
| film-1920x804  | 1800 | trt32 | 14.3 | 45.44 | 0.98249 | 102.84 | 1.0 | 0.0011 |
| film-1920x804  | 1800 | trt16 | 11.8 | 44.53 | 0.98032 | 56.17 | 0.99878 | 0.0638 |
| film-1920x804  | 3600 | blend |  | 38.03 | 0.95109 |  |  |  |
| film-1920x804  | 3600 | cuda32 | 19.5 | 40.61 | 0.95607 |  |  |  |
| film-1920x804  | 3600 | trt32 | 14.2 | 40.61 | 0.95607 | 96.15 | 1.0 | 0.0014 |
| film-1920x804  | 3600 | trt16 | 11.5 | 40.18 | 0.9554 | 48.46 | 0.99647 | 0.1286 |
| film-1920x804  | 7200 | blend |  | 35.66 | 0.95717 |  |  |  |
| film-1920x804  | 7200 | cuda32 | 19.6 | 33.28 | 0.92245 |  |  |  |
| film-1920x804  | 7200 | trt32 | 14.2 | 33.28 | 0.92246 | 100.71 | 1.0 | 0.0004 |
| film-1920x804  | 7200 | trt16 | 11.8 | 33.13 | 0.92139 | 52.05 | 0.99759 | 0.1486 |
| music-video-3840x2160 | 30 | blend |  | 26.14 | 0.95725 |  |  |  |
| music-video-3840x2160 | 30 | cuda32 | 134.3 | 23.61 | 0.90489 |  |  |  |
| music-video-3840x2160 | 30 | trt32 | 87.3 | 23.61 | 0.90489 | 88.19 | 1.0 | 0.0051 |
| music-video-3840x2160 | 30 | trt16 | 70.1 | 23.45 | 0.90341 | 34.75 | 0.96996 | 0.842 |
| music-video-3840x2160 | 90 | blend |  | 17.55 | 0.91627 |  |  |  |
| music-video-3840x2160 | 90 | cuda32 | 111.3 | 24.84 | 0.96392 |  |  |  |
| music-video-3840x2160 | 90 | trt32 | 79.4 | 24.85 | 0.96393 | 75.17 | 0.99999 | 0.0293 |
| music-video-3840x2160 | 90 | trt16 | 61.4 | 24.73 | 0.96273 | 47.08 | 0.99564 | 0.2168 |
| music-video-3840x2160 | 150 | blend |  | 14.88 | 0.87146 |  |  |  |
| music-video-3840x2160 | 150 | cuda32 | 111.0 | 13.62 | 0.82462 |  |  |  |
| music-video-3840x2160 | 150 | trt32 | 79.5 | 13.62 | 0.82465 | 69.99 | 0.99998 | 0.0074 |
| music-video-3840x2160 | 150 | trt16 | 61.8 | 13.61 | 0.82415 | 52.17 | 0.99935 | 0.1951 |
| music-video-3840x2160 | 200 | blend |  | 22.69 | 0.94573 |  |  |  |
| music-video-3840x2160 | 200 | cuda32 | 111.5 | 20.51 | 0.90671 |  |  |  |
| music-video-3840x2160 | 200 | trt32 | 79.5 | 20.51 | 0.90672 | 86.85 | 1.0 | 0.0113 |
| music-video-3840x2160 | 200 | trt16 | 61.6 | 20.56 | 0.90733 | 37.62 | 0.9887 | 0.6598 |
