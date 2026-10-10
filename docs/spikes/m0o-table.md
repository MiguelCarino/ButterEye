| method | licence | ms/frame 1080p→4K | extra vs bilinear |
|---|---|---|---|
| bilinear | libplacebo (LGPL-2.1+) | 8.86 | -0.15 |
| spline36 | libplacebo | 9.31 | 0.31 |
| lanczos | libplacebo | 9.37 | 0.36 |
| ewa_lanczos | libplacebo | 9.63 | 0.63 |
| ewa_lanczossharp | libplacebo | 9.67 | 0.67 |
| ewa_lanczos4sharpest | libplacebo | 9.95 | 0.95 |
| FSRCNNX_x2_8 | GPL-3.0 | 10.73 | 1.73 |
| FSRCNNX_x2_16 | GPL-3.0 | 12.36 | 3.36 |
| ravu-lite-ar-r4 | LGPL-3.0 | 10.17 | 1.17 |
| ravu-r3-yuv | LGPL-3.0 | 10.06 | 1.05 |
| ArtCNN_C4F16 | MIT | 11.87 | 2.86 |
| Anime4K_CNN_x2_M | MIT | 9.77 | 0.77 |

| clip | t | method | PSNR | luma PSNR | SSIM |
|---|---|---|---|---|---|
| anime-film-a | 600 | bilinear | 45.82 | 46.43 | 0.98921 |
| anime-film-a | 600 | spline36 | 48.47 | 49.67 | 0.99245 |
| anime-film-a | 600 | lanczos | 48.61 | 49.85 | 0.99257 |
| anime-film-a | 600 | ewa_lanczos | 48.41 | 49.56 | 0.99212 |
| anime-film-a | 600 | ewa_lanczossharp | 48.5 | 49.66 | 0.99225 |
| anime-film-a | 600 | ewa_lanczos4sharpest | 48.54 | 49.71 | 0.99239 |
| anime-film-a | 600 | FSRCNNX_x2_8 | 48.99 | 50.42 | 0.99306 |
| anime-film-a | 600 | FSRCNNX_x2_16 | 49.24 | 50.77 | 0.99334 |
| anime-film-a | 600 | ravu-lite-ar-r4 | 48.62 | 49.9 | 0.99225 |
| anime-film-a | 600 | ravu-r3-yuv | 42.79 | 43.16 | 0.98671 |
| anime-film-a | 600 | ArtCNN_C4F16 | 48.91 | 50.35 | 0.99274 |
| anime-film-a | 600 | Anime4K_CNN_x2_M | 47.35 | 48.36 | 0.99073 |
| anime-film-a | 1800 | bilinear | 42.53 | 43.64 | 0.98867 |
| anime-film-a | 1800 | spline36 | 45.71 | 47.71 | 0.99241 |
| anime-film-a | 1800 | lanczos | 45.84 | 47.91 | 0.99246 |
| anime-film-a | 1800 | ewa_lanczos | 45.52 | 47.5 | 0.99185 |
| anime-film-a | 1800 | ewa_lanczossharp | 45.62 | 47.61 | 0.992 |
| anime-film-a | 1800 | ewa_lanczos4sharpest | 45.87 | 47.83 | 0.99276 |
| anime-film-a | 1800 | FSRCNNX_x2_8 | 46.17 | 48.64 | 0.99288 |
| anime-film-a | 1800 | FSRCNNX_x2_16 | 46.35 | 49.0 | 0.99319 |
| anime-film-a | 1800 | ravu-lite-ar-r4 | 45.92 | 48.15 | 0.99251 |
| anime-film-a | 1800 | ravu-r3-yuv | 38.12 | 38.74 | 0.98244 |
| anime-film-a | 1800 | ArtCNN_C4F16 | 46.02 | 48.44 | 0.99266 |
| anime-film-a | 1800 | Anime4K_CNN_x2_M | 44.55 | 46.22 | 0.9906 |
| anime-film-a | 3000 | bilinear | 41.29 | 41.7 | 0.98231 |
| anime-film-a | 3000 | spline36 | 44.43 | 45.34 | 0.98939 |
| anime-film-a | 3000 | lanczos | 44.61 | 45.59 | 0.98968 |
| anime-film-a | 3000 | ewa_lanczos | 44.36 | 45.28 | 0.98898 |
| anime-film-a | 3000 | ewa_lanczossharp | 44.47 | 45.4 | 0.98922 |
| anime-film-a | 3000 | ewa_lanczos4sharpest | 44.41 | 45.28 | 0.98933 |
| anime-film-a | 3000 | FSRCNNX_x2_8 | 45.04 | 46.3 | 0.99103 |
| anime-film-a | 3000 | FSRCNNX_x2_16 | 45.2 | 46.51 | 0.99142 |
| anime-film-a | 3000 | ravu-lite-ar-r4 | 44.34 | 45.34 | 0.98916 |
| anime-film-a | 3000 | ravu-r3-yuv | 37.76 | 37.96 | 0.9759 |
| anime-film-a | 3000 | ArtCNN_C4F16 | 45.09 | 46.42 | 0.99067 |
| anime-film-a | 3000 | Anime4K_CNN_x2_M | 43.48 | 44.37 | 0.98763 |
| anime-film-b | 1200 | bilinear | 45.26 | 45.71 | 0.99272 |
| anime-film-b | 1200 | spline36 | 46.71 | 47.34 | 0.99494 |
| anime-film-b | 1200 | lanczos | 46.76 | 47.4 | 0.99499 |
| anime-film-b | 1200 | ewa_lanczos | 46.64 | 47.25 | 0.9946 |
| anime-film-b | 1200 | ewa_lanczossharp | 46.68 | 47.29 | 0.99472 |
| anime-film-b | 1200 | ewa_lanczos4sharpest | 46.49 | 47.04 | 0.99493 |
| anime-film-b | 1200 | FSRCNNX_x2_8 | 47.41 | 48.19 | 0.99522 |
| anime-film-b | 1200 | FSRCNNX_x2_16 | 47.66 | 48.52 | 0.99554 |
| anime-film-b | 1200 | ravu-lite-ar-r4 | 46.39 | 46.98 | 0.99486 |
| anime-film-b | 1200 | ravu-r3-yuv | 42.79 | 43.13 | 0.99024 |
| anime-film-b | 1200 | ArtCNN_C4F16 | 47.32 | 48.14 | 0.99494 |
| anime-film-b | 1200 | Anime4K_CNN_x2_M | 46.64 | 47.3 | 0.99334 |
| anime-film-b | 3600 | bilinear | 32.57 | 33.5 | 0.91984 |
| anime-film-b | 3600 | spline36 | 34.53 | 36.15 | 0.94396 |
| anime-film-b | 3600 | lanczos | 34.68 | 36.34 | 0.94584 |
| anime-film-b | 3600 | ewa_lanczos | 34.14 | 35.71 | 0.93926 |
| anime-film-b | 3600 | ewa_lanczossharp | 34.26 | 35.83 | 0.9411 |
| anime-film-b | 3600 | ewa_lanczos4sharpest | 34.66 | 36.26 | 0.94537 |
| anime-film-b | 3600 | FSRCNNX_x2_8 | 35.42 | 37.82 | 0.95016 |
| anime-film-b | 3600 | FSRCNNX_x2_16 | 35.55 | 38.06 | 0.95097 |
| anime-film-b | 3600 | ravu-lite-ar-r4 | 34.99 | 36.99 | 0.94652 |
| anime-film-b | 3600 | ravu-r3-yuv | 30.95 | 31.66 | 0.90865 |
| anime-film-b | 3600 | ArtCNN_C4F16 | 35.19 | 37.54 | 0.94804 |
| anime-film-b | 3600 | Anime4K_CNN_x2_M | 34.59 | 36.67 | 0.93922 |
| live-action-4k | 30 | bilinear | 39.57 | 39.58 | 0.98504 |
| live-action-4k | 30 | spline36 | 41.49 | 41.52 | 0.98979 |
| live-action-4k | 30 | lanczos | 41.67 | 41.7 | 0.99017 |
| live-action-4k | 30 | ewa_lanczos | 41.37 | 41.39 | 0.98913 |
| live-action-4k | 30 | ewa_lanczossharp | 41.47 | 41.5 | 0.98942 |
| live-action-4k | 30 | ewa_lanczos4sharpest | 41.6 | 41.63 | 0.99005 |
| live-action-4k | 30 | FSRCNNX_x2_8 | 43.25 | 43.3 | 0.9919 |
| live-action-4k | 30 | FSRCNNX_x2_16 | 43.33 | 43.38 | 0.99242 |
| live-action-4k | 30 | ravu-lite-ar-r4 | 42.13 | 42.16 | 0.99081 |
| live-action-4k | 30 | ravu-r3-yuv | 38.78 | 38.84 | 0.9844 |
| live-action-4k | 30 | ArtCNN_C4F16 | 43.27 | 43.32 | 0.99144 |
| live-action-4k | 30 | Anime4K_CNN_x2_M | 42.58 | 42.62 | 0.99002 |
| live-action-4k | 90 | bilinear | 47.58 | 47.67 | 0.99725 |
| live-action-4k | 90 | spline36 | 48.57 | 48.66 | 0.9979 |
| live-action-4k | 90 | lanczos | 48.63 | 48.73 | 0.99794 |
| live-action-4k | 90 | ewa_lanczos | 48.5 | 48.58 | 0.99774 |
| live-action-4k | 90 | ewa_lanczossharp | 48.53 | 48.61 | 0.99778 |
| live-action-4k | 90 | ewa_lanczos4sharpest | 48.17 | 48.24 | 0.99793 |
| live-action-4k | 90 | FSRCNNX_x2_8 | 49.56 | 49.67 | 0.99753 |
| live-action-4k | 90 | FSRCNNX_x2_16 | 49.12 | 49.23 | 0.99811 |
| live-action-4k | 90 | ravu-lite-ar-r4 | 47.85 | 47.93 | 0.99785 |
| live-action-4k | 90 | ravu-r3-yuv | 46.02 | 46.32 | 0.99643 |
| live-action-4k | 90 | ArtCNN_C4F16 | 50.54 | 50.68 | 0.99737 |
| live-action-4k | 90 | Anime4K_CNN_x2_M | 49.38 | 49.49 | 0.99754 |
