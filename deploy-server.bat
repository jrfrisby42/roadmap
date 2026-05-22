@echo off
setlocal

set KEY=C:\Users\JRFrisby\.ssh\frazil-app.pem
set HOST=ubuntu@52.35.224.183

echo Backing up files...

ssh -i "%KEY%" %HOST% "sudo mkdir -p /opt/roadmap/bkup && sudo cp /opt/roadmap/server.py /opt/roadmap/bkup/server-$(date +%%Y%%m%%d-%%H%%M%%S).py"
timeout /t 1 /nobreak >nul

echo Uploading files...
scp -i "%KEY%" server.py %HOST%:/opt/roadmap/

echo Restarting service...
ssh -i "%KEY%" %HOST% "sudo systemctl restart roadmap"

echo Done!
endlocal