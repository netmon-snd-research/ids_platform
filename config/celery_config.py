"""
Celery configuration.

Centralized config so both the worker and experiment_service
use the same broker/backend settings.

CONCURRENCY NOTE:
  CELERY_CONCURRENCY defaults to 1: one experiment at a time. The default is
  conservative, not a design limit. It fits the development laptop, where the
  worker gets a 3.5 GB cap and three concurrent SVC runs on 500K rows would
  OOM. A server with real RAM can and should run several at once.

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

# Default 1: one at a time. See CONCURRENCY NOTE above before raising it.
CELERY_CONCURRENCY = int(os.environ.get("CELERY_CONCURRENCY", "1"))
