@echo off
echo ============================================
echo  Environ Chatbot -- Indexing knowledge base
echo ============================================
if not exist "data" mkdir data
copy /Y "C:\Users\Crown Tech\Downloads\Cleaned_Data.txt" "data\knowledge_base.txt"
echo Copied knowledge base.
echo.
python ingest.py
echo.
echo Done! Now run start.bat to launch the chatbot.
pause
