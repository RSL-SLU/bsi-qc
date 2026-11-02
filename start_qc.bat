@echo off
cd /d "%~dp0"
python bsi_qc.py serve %*
pause
