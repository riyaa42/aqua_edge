#!/bin/sh
# Make the password file from compose.yaml's environment, then run the broker.
set -eu
dir=/tmp/aqua
rm -rf "$dir"      # left from before a restart; mosquitto_passwd will not overwrite it
mkdir -p "$dir"
umask 077
mosquitto_passwd -c -b "$dir/passwords" gateway "$MQTT_GATEWAY_PASSWORD"
mosquitto_passwd -b "$dir/passwords" viewer "$MQTT_VIEWER_PASSWORD"
cp /config/acl "$dir/acl"
chown -R mosquitto:mosquitto "$dir" /mosquitto/data      # the broker drops to this user
exec mosquitto -c /config/mosquitto.conf
