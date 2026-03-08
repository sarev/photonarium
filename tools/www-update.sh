#!/usr/bin/env bash

GENERATED=./generated
IMAGES=./www/images
TUTORIAL=./www/tutorial

if [ -d ${GENERATED}/screenshots ]; then
    cp -fv ${GENERATED}/screenshots/*.png ${TUTORIAL}/screenshots/
fi
if [ -f ${GENERATED}/index.html ]; then
    cp -fv ${GENERATED}/index.html ${TUTORIAL}/
fi
if [ -d ${IMAGES} ]; then
    cp -fv ${TUTORIAL}/screenshots/4-4.png  ${IMAGES}/duplicates.png
    cp -fv ${TUTORIAL}/screenshots/6-14.png ${IMAGES}/faces.png
    cp -fv ${TUTORIAL}/screenshots/2-1.png  ${IMAGES}/fullscreen.png
    cp -fv ${TUTORIAL}/screenshots/2-5.png  ${IMAGES}/gallery.png
    cp -fv ${TUTORIAL}/screenshots/1-4.png  ${IMAGES}/info-panel.png
    cp -fv ${TUTORIAL}/screenshots/8-8.png  ${IMAGES}/heatmap.png
    cp -fv ${TUTORIAL}/screenshots/9-2.png  ${IMAGES}/light-theme.png
fi
