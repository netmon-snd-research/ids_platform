"""
Celery configuration.

Centralized config so both the worker and experiment_service
use the same broker/backend settings.

CONCURRENCY NOTE:
  docker-compose.yml ships CELERY_CONCURRENCY=4: four experiments at once,
  the lab server this repository serves. The fallback below stays 1 because
  it describes the other situation, running the UI or a worker directly
  without compose, where nothing is parallel anyway. Under compose the
  variable is always passed explicitly, so the two cannot disagree in
  practice.

  A smaller machine must lower it in `.env`. The development laptop used
  CELERY_CONCURRENCY=1 with a 3.5 GB cap, where three concurrent SVC runs on
  500K rows would OOM.

  Raise it in `.env` (docker-compose.yml reads the same variable for both
  `--concurrency` and this setting, so the two cannot drift). Two things move
  with it:

  * the worker pool. `--pool=solo` never runs anything in parallel whatever
    this value says, so compose defaults to `prefork`;
  * the per-run RAM budget. `mem_limit` applies to the WHOLE container, so N
    concurrent runs share it. `ui/views/run_experiment.py` divides
    WORKER_MEM_LIMIT_MB by this number before deciding which datasets are too
    big to start.

  Sizing rule: WORKER_MEM_LIMIT_MB / CELERY_CONCURRENCY should stay comfortably
  above the heaviest pipeline's peak RSS, which is 2.7 to 3.7 GB for the EVE
  cbr adapter at modeling_train_rows=150_000.
"""
import os

CELERY_BROKER_URL = os.environ.get("CELERY_BROKER_URL", "redis://localhost:6379/0")
CELERY_RESULT_BACKEND = os.environ.get("CELERY_RESULT_BACKEND", "redis://localhost:6379/1")

# Set to True to use async (Celery), False to use sync (local_worker)
USE_ASYNC = os.environ.get("USE_ASYNC", "false").lower() == "true"

# Fallback for running outside compose. Compose ships 4; see note above.
CELERY_CONCURRENCY = int(os.environ.get("CELERY_CONCURRENCY", "1"))
