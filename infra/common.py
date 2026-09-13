# -*- coding: utf-8 -*-
"""共用工具：節流 boto3 client、state.json 讀寫、log 輔助。"""
import json
import os
import time
import boto3
from botocore.config import Config

REGION = "us-west-2"
PREFIX = "ntpc-appeals"
CFG = Config(region_name=REGION, retries={"max_attempts": 5, "mode": "standard"})

_STATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")


def client(service):
    return boto3.client(service, config=CFG)


def resource(service):
    return boto3.resource(service, config=CFG)


def slp(sec=0.5):
    time.sleep(sec)


def load_state():
    if os.path.exists(_STATE_PATH):
        with open(_STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(_STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def set_state(key, value):
    s = load_state()
    s[key] = value
    save_state(s)
    return s


def get_account_id():
    s = load_state()
    if "account_id" in s:
        return s["account_id"]
    acc = client("sts").get_caller_identity()["Account"]
    set_state("account_id", acc)
    return acc


class Logger:
    def __init__(self, path):
        self.path = os.path.join(os.path.dirname(os.path.abspath(__file__)), path)
        self.lines = []

    def log(self, x=""):
        print(x)
        self.lines.append(str(x))

    def flush(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("\n".join(self.lines))
