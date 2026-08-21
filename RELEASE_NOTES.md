# ZMK Vision v2.2.2 — CPU Fallback & NVIDIA Runtime Detection

Исправлена ошибка запуска:

`could not select device driver "nvidia" with capabilities: [[gpu]]`

- GPU reservation удалён из базового `docker-compose.yml`.
- Inference и training workers всегда запускаются на CPU, если NVIDIA runtime отсутствует.
- Добавлен `docker-compose.gpu.yml` с `gpus: all` для машин с NVIDIA Container Toolkit.
- Windows/Linux installers автоматически проверяют Docker runtimes и подключают GPU override только при наличии `nvidia`.
- `INFERENCE_DEVICE=auto` и `TRAINING_DEVICE=auto` выбирают CUDA при доступности, иначе CPU.
- Capability API считает training worker рабочим и в CPU-режиме, отдельно возвращая `gpu` и `device`.
- Образы больше не падают на старте на компьютерах без NVIDIA.
