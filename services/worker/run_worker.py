#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Celery worker entrypoint. QUEUES из env CELERY_QUEUES (parse,index | audit)."""
import os

from app.agent.tasks import celery_app

if __name__ == "__main__":
    queues = os.environ.get("CELERY_QUEUES", "index,audit")
    concurrency = int(os.environ.get("CELERY_WORKER_CONCURRENCY", "2"))
    celery_app.worker_main([
        "worker",
        f"--queues={queues}",
        f"--concurrency={concurrency}",
        "--loglevel=INFO",
    ])
