#!/bin/bash
# First-boot script for the AirBreda VM (Amazon Linux 2023). Amazon Linux 2023 ships without cron, hence cronie.
dnf install -y docker git cronie postgresql16
systemctl enable --now docker crond
usermod -aG docker ec2-user
