# ZMK Vision v2.2.4 — Final Worker Race Fixes

- Исправлено смешивание tracker state между разными камерами: inference использует независимый spatial person ID по camera/class/position.
- Устранена гонка multiprocessing Queue: training worker ждёт terminal message после завершения child process и не оставляет задачу в running.
- Исправлена реальная cancellation и cleanup process/queue.
- Camera preview больше не обновляет React state после unmount и освобождает Object URL при позднем ответе.
- Повторно проверены snapshot upload/get/delete, camera delete, CPU fallback, inference и training lifecycle.
