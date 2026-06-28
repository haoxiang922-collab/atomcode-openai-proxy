@echo off
title AtomCode Proxy (port 8787)
cd /d D:\xunlei\cc\atomcode-proxy

echo ============================================
echo  AtomCode OpenAI/Claude Proxy
echo  Listen: http://127.0.0.1:8787
echo  Endpoints: /v1/chat/completions  /v1/messages
echo  Models: glm-5.2 / deepseek-v4-flash / qwen3-vl-8b-instruct
echo ============================================
echo.
echo [starting...] close this window to stop
echo.

"C:\Users\zhx\AppData\Local\Python\bin\python.exe" server.py

echo.
echo [stopped] press any key to close
pause >nul
