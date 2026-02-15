@echo off
echo Starting The First Client Engine...
echo.
echo Installing requirements...
pip install -r requirements.txt
echo.
echo Starting Flask server...
echo Access the app at: http://localhost:5000
echo.
python app.py
pause