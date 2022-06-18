#!/usr/bin/env python3

import asyncio
import platform

from src.main import main

# Lightweight wrapper around main.py
if platform.system() == "Windows":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
asyncio.run(main())
