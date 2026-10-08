from typing import Any, Union
import os

import google.cloud.secretmanager as secretmanager
from .plugin_exception import PluginException

class GCPException(PluginException):
    pass

def get_secrets() -> Union[None, dict[str, dict[str, str]]]:

    project_id = os.getenv("GCP_PROJECT_ID")
    if project_id is None:
        raise GCPException((f"Couldn't find GCP_PROJECT_ID from env variables!"))

    secret_env_keys: list[str] = ["SLACK_API_SECRET_ID", "SLACK_APP_SECRET_ID"]
    env_cfg_keys: list[str] = ["CHANNEL", "CONTROL", "EMAIL", "STORAGE", "SLACK_ADMIN_CHANNEL_ID"]
    JSON_dict: dict[str, dict[str, Any]] = {}

    secret_manager = secretmanager.SecretManagerServiceClient()
    for env_key in secret_env_keys:
        secret_id = os.getenv(env_key)
        if secret_id is None:
            raise GCPException((f"Couldn't find {env_key} from env variables!"))
        try:
            response = secret_manager.access_secret_version(request={
                "name": f"projects/{project_id}/secrets/{secret_id}/versions/latest",
            })
        except Exception as exn:
            raise GCPException((f"Error in fetching secrets: {exn.args[0]}"))
        secret_value = response.payload.data.decode("utf-8")
        if not "slack" in JSON_dict:
            JSON_dict["slack"] = {}
        JSON_dict["slack"][env_key] = secret_value

    for key in env_cfg_keys:
        if key == "EMAIL":
            if not "tesla" in JSON_dict:
                JSON_dict["tesla"] = {}
            JSON_dict["tesla"][key.lower()] = os.getenv(key)
        elif key == "CONTROL" or key == "STORAGE":
            if not "common" in JSON_dict:
                JSON_dict["common"] = {}
            JSON_dict["common"][key.lower()] = os.getenv(key)
        else:
            JSON_dict["slack"][key.lower()] = os.getenv(key)

    return JSON_dict
