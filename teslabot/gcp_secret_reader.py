from typing import List, Dict, Any, Union
import os

from google.cloud import secretmanager

def get_secrets() -> Union[None, Dict[str, Dict[str, str]]]:

    project_id = os.getenv("GCP_PROJECT_ID")
    if project_id is None: return None
    
    secret_env_keys: List[str] = ["SLACK_API_SECRET_ID", "SLACK_APP_SECRET_ID"]
    env_cfg_keys: List[str] = ["CHANNEL", "CONTROL", "EMAIL", "STORAGE"]
    JSON_dict: Dict[str, Dict[str, Any]] = {}

    secret_manager = secretmanager.SecretManagerServiceClient()
    for env_key in secret_env_keys:
        secret_id = os.getenv(env_key)
        response = secret_manager.access_secret_version(request={
            "name": f"projects/{project_id}/secrets/{secret_id}/versions/latest",
        })
        secret_value = response.payload.data.decode("utf-8")
        if not "slack" in JSON_dict:
            JSON_dict["slack"] = {}
        JSON_dict["slack"][secret_id] = secret_value
    
    for key in env_cfg_keys:
        if key == "EMAIL":
            if not "tesla" in JSON_dict:
                JSON_dict["tesla"] = {}
            JSON_dict["tesla"][key] = os.getenv(key)
        elif key == ("CONTROL" or "STORAGE"):
            if not "common" in JSON_dict:
                JSON_dict["common"] = {}
            JSON_dict["common"][key] = os.getenv(key)
        else:
            JSON_dict["slack"][key] = os.getenv(key)

    return JSON_dict
