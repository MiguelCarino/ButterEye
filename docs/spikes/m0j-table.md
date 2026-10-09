| model | size | runtime | provider | gpu fps | host fps | setup s |
|---|---|---|---|---|---|---|
| rife_v4.26 | 1080p | cuda | CUDAExecutionProvider | 55.5 | 38.8 | 1.1 |
| rife_v4.26 | 1080p | trt-fp32 | TensorrtExecutionProvider | 92.8 | 54.6 | 20.2 |
| rife_v4.26 | 1080p | trt-fp16 | TensorrtExecutionProvider | 148.3 | 69.6 | 39.1 |
| rife_v4.26 | 2160p | cuda | CUDAExecutionProvider | 12.6 | 9.0 | 0.6 |
| rife_v4.26 | 2160p | trt-fp32 | TensorrtExecutionProvider | 21.2 | 12.5 | 29.1 |
| rife_v4.26 | 2160p | trt-fp16 | TensorrtExecutionProvider | 33.5 | 16.2 | 52.0 |
| rife_v4.22_lite | 1080p | cuda | CUDAExecutionProvider | 61.6 | 41.9 | 0.2 |
| rife_v4.22_lite | 1080p | trt-fp32 | TensorrtExecutionProvider | 103.5 | 58.0 | 4.5 |
| rife_v4.22_lite | 1080p | trt-fp16 | TensorrtExecutionProvider | 174.0 | 75.2 | 7.9 |
| rife_v4.22_lite | 2160p | cuda | CUDAExecutionProvider | 14.3 | 9.8 | 0.5 |
| rife_v4.22_lite | 2160p | trt-fp32 | TensorrtExecutionProvider | 23.6 | 13.4 | 5.6 |
| rife_v4.22_lite | 2160p | trt-fp16 | TensorrtExecutionProvider | 38.8 | 17.2 | 9.5 |
