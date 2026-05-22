@echo off
setlocal

set KEY=C:\Users\JRFrisby\.ssh\frazil-app.pem
set HOST=ubuntu@52.35.224.183

echo Backing up files...

ssh -i "%KEY%" %HOST% "sudo mkdir -p /opt/roadmap/bkup && sudo cp /opt/roadmap/roadmap.html /opt/roadmap/bkup/roadmap-$(date +%%Y%%m%%d-%%H%%M%%S).html"
timeout /t 1 /nobreak >nul

echo Uploading files...
scp -i "%KEY%" roadmap.html %HOST%:/opt/roadmap/

echo Done!
endlocal