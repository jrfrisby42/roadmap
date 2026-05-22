@echo off
echo Restarting service...
ssh -i C:\Users\JRFrisby\.ssh\frazil-app.pem ubuntu@52.35.224.183 "sudo systemctl restart roadmap"
echo Done!