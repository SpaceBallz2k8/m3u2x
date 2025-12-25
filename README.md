This is a simple "fake" xtremecodes API server
I made this to accept m3u files/url's that contain group info (extended m3u info)
It adds them to a "fake" xtreamcodes server that you can add to dispatcharr which add's VOD items
The VOD items are streamed direct from their links
This means you can use a tool like m3unator to create an m3u from an open directory of media items and then add that m3u to the "fake" proxy. They will then show up in your dispatcharr server

make a folder

clone the repo

set your desired port in the .env

docker compose build

docker compose up -d


visit the web ui at http://YOUR_DOCKER_IP:YOUR_PORT
