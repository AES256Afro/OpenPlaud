#!/bin/sh
# Stands in for xdg-open: saves the URL Plaud wants opened so the web app can show it as a link.
printf '%s' "$1" > "${OPENPLAUD_AUTH_URL_FILE:-/tmp/openplaud-auth-url}"
