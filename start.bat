@echo off
chcp 65001 >nul
title WocaBee Bot
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
python -u startup.py
exit
