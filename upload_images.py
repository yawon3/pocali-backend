import os
import requests

BACKEND = "https://pocali-backend.onrender.com"
# 실제 이미지들이 들어있는 최상위 폴더 경로
BASE_DIR = os.path.join(os.getcwd(), "static", "images")

def main():
    for subdir, _, files in os.walk(BASE_DIR):
        file_type = os.path.relpath(subdir, BASE_DIR)
        for fname in files:
            path = os.path.join(subdir, fname)
            name, ext = os.path.splitext(fname)
            with open(path, "rb") as f:
                # custom_filename 필드를 추가!
                data = {
                    "file_type": file_type,
                    "custom_filename": name  # 확장자 빼고 원본 이름
                }
                res = requests.post(
                    f"{BACKEND}/admin/upload",
                    files={"file": f},
                    data=data
                )
            # 응답이 JSON이 아닐 수도 있으니 텍스트로 찍어 봅니다
            print(fname, res.status_code, res.text[:200])

if __name__ == "__main__":
    main()
