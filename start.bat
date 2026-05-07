@echo off
echo ============================================
echo  Environ Chatbot -- Starting server
echo  Open: http://localhost:8000
echo ============================================
uvicorn main:app --reload --host 0.0.0.0 --port 8000
