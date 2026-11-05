#!/usr/bin/env bash
set -euo pipefail
DST=/tmp/sentry-validation-extra-fixtures
mkdir -p "$DST"
ffmpeg -v error -threads 2 -i /home/admin1/Downloads/camera_recordings_2026-10-12/Camera12_192.168.1.13_2min_h264.mp4 -t 20 -an -vf scale=1280:720,fps=10 -c:v libx264 -preset veryfast -crf 20 -threads 2 -g 10 -bf 0 -y "$DST/quiet720.mp4"
ffmpeg -v error -threads 2 -i /home/admin1/Downloads/Usecase-Catalog-main/videos/crowd.mp4 -an -vf scale=1280:720,fps=10 -c:v libx264 -preset veryfast -crf 20 -threads 2 -g 10 -bf 0 -y "$DST/active720.mp4"
ffmpeg -v error -threads 2 -i /home/admin1/Downloads/Usecase-Catalog-main/videos/fire_smoke.mp4 -t 20 -an -vf scale=1280:720,fps=10 -c:v libx264 -preset veryfast -crf 20 -threads 2 -g 10 -bf 0 -y "$DST/fire720.mp4"
printf "file '%s'\nfile '%s'\nfile '%s'\n" "$DST/quiet720.mp4" "$DST/active720.mp4" "$DST/quiet720.mp4" > "$DST/motion.txt"
ffmpeg -v error -f concat -safe 0 -i "$DST/motion.txt" -c copy -y "$DST/motion-transition.mp4"
printf "file '%s'\nfile '%s'\nfile '%s'\n" "$DST/fire720.mp4" "$DST/quiet720.mp4" "$DST/quiet720.mp4" > "$DST/fire.txt"
ffmpeg -v error -f concat -safe 0 -i "$DST/fire.txt" -c copy -y "$DST/fire-clear.mp4"
ffmpeg -v error -threads 2 -i /tmp/sentry-resolution-fixtures/crowd-1080p.mp4 -an -c:v libx265 -preset veryfast -crf 22 -threads 2 -x265-params pools=2:frame-threads=2:log-level=error -y "$DST/crowd-1080p-hevc.mp4"
ffmpeg -v error -threads 2 -i /home/admin1/Downloads/Usecase-Catalog-main/videos/crowd.mp4 -an -vf crop=400:720:440:0,scale=720:1280 -c:v libx264 -preset veryfast -crf 20 -threads 2 -y "$DST/portrait.mp4"
ffmpeg -v error -threads 2 -i /home/admin1/Downloads/Usecase-Catalog-main/videos/crowd.mp4 -an -vf scale=1278:718 -c:v libx264 -preset veryfast -crf 20 -threads 2 -y "$DST/nonaligned.mp4"
ffmpeg -v error -threads 2 -i /home/admin1/Downloads/Usecase-Catalog-main/videos/crowd.mp4 -an -vf fps=15 -c:v libx264 -preset veryfast -crf 20 -threads 2 -y "$DST/fps15.mp4"
ffmpeg -v error -threads 2 -i /home/admin1/Downloads/Usecase-Catalog-main/videos/crowd.mp4 -an -vf fps=25 -c:v libx264 -preset veryfast -crf 20 -threads 2 -y "$DST/fps25.mp4"
