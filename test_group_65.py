import json
import shutil
import os
from fb_poster import run_post

def main():
    print("Copying session data to avoid interfering with running script...")
    os.system("cp -R fb_session fb_session_temp 2>/dev/null")
    
    # Remove lockfiles if they exist
    lockfiles = ["fb_session_temp/SingletonLock", "fb_session_temp/SingletonCookie"]
    for lf in lockfiles:
        if os.path.exists(lf):
            try:
                os.remove(lf)
            except Exception:
                pass

    print("Creating temporary config for group 65...")
    with open("config.json", "r") as f:
        config = json.load(f)
        
    target_url = "https://www.facebook.com/groups/458070877541419/"
    config["groups"] = [g for g in config["groups"] if g.get("url") == target_url]
    if config["groups"]:
        config["groups"][0]["enabled"] = True
    else:
        print("Could not find the target group in config.json")
        return
        
    with open("config_temp.json", "w") as f:
        json.dump(config, f)
        
    print("Running poster in test mode for isolated group...")
    try:
        run_post("fb_session_temp", "config_temp.json", test_mode=True)
    finally:
        # Cleanup
        print("Cleaning up temp files...")
        if os.path.exists("config_temp.json"):
            os.remove("config_temp.json")
        shutil.rmtree("fb_session_temp", ignore_errors=True)

if __name__ == "__main__":
    main()
