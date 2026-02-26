import os
import glob
import mimetypes
import time
import json
from tqdm import tqdm
import firebase_admin
from firebase_admin import credentials, storage

# Firebase 인증 및 초기화
def initialize_firebase():
    # 서비스 계정 키 파일 경로 (Firebase 콘솔에서 다운로드)
    cred = credentials.Certificate("serviceAccountKey.json")
    firebase_admin.initialize_app(cred, {
        'storageBucket': 'pocali.firebasestorage.app'
    })
    return storage.bucket()

# 이미지 파일 찾기
def find_image_files(source_dir):
    # 이미지 확장자 목록
    image_extensions = ['.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp', '.svg']
    
    # 모든 파일 가져오기
    all_files = []
    for ext in image_extensions:
        all_files.extend(glob.glob(f"{source_dir}/**/*{ext}", recursive=True))
        all_files.extend(glob.glob(f"{source_dir}/**/*{ext.upper()}", recursive=True))
    
    print(f"총 {len(all_files)}개의 이미지 파일을 찾았습니다.")
    return all_files

# 파일 업로드
def upload_file(bucket, local_path, destination_path, skip_existing=True):
    try:
        # 파일명 검증 - 원본 파일명이 유지되는지 확인
        original_filename = os.path.basename(local_path)
        dest_filename = os.path.basename(destination_path)
        
        if original_filename != dest_filename:
            raise ValueError(f"파일명이 변경되었습니다! 원본: {original_filename}, 대상: {dest_filename}")
        
        # 파일 존재 여부 확인
        blob = bucket.blob(destination_path)
        if skip_existing and blob.exists():
            return {'success': True, 'skipped': True, 'path': destination_path, 'message': '이미 존재함'}
        
        # MIME 타입 추측
        content_type, _ = mimetypes.guess_type(local_path)
        if not content_type and local_path.lower().endswith('.jpg'):
            content_type = 'image/jpeg'
        elif not content_type:
            content_type = 'application/octet-stream'
        
        # 메타데이터 설정
        metadata = {'contentType': content_type}
        
        # 업로드
        blob.upload_from_filename(local_path, content_type=content_type)
        
        # 공개 URL 생성
        blob.make_public()
        public_url = blob.public_url
        
        return {
            'success': True, 
            'skipped': False, 
            'path': destination_path,
            'url': public_url,
            'message': '업로드 성공'
        }
        
    except Exception as e:
        return {
            'success': False, 
            'path': local_path, 
            'error': str(e),
            'message': '업로드 실패'
        }

# 메인 함수
def main():
    # !!! 주의: 파일명과 폴더 구조는 반드시 유지되어야 합니다 !!!
    # !!! 웹앱에서 정렬과 오버레이 표시에 영향을 줍니다 !!!
    
    # 설정
    config = {
        'source_dir': 'E:\pocali-backend\static\images',           # 로컬 이미지 폴더
        'destination_prefix': 'images',     # Firebase 저장 경로 접두사
        'preserve_structure': True,         # 폴더 구조 유지 여부 (변경 금지)
        'skip_existing': True               # 이미 존재하는 파일 건너뛰기
    }
    
    # 폴더 구조 유지 설정을 강제로 True로 설정
    if not config['preserve_structure']:
        print("경고: 폴더 구조 유지 설정이 False로 되어 있습니다. 웹앱 호환성을 위해 True로 변경합니다.")
        config['preserve_structure'] = True
    
    # Firebase 초기화
    print("Firebase 초기화 중...")
    bucket = initialize_firebase()
    
    # 이미지 파일 찾기
    print(f"{config['source_dir']} 폴더에서 이미지 검색 중...")
    files = find_image_files(config['source_dir'])
    
    if not files:
        print("업로드할 이미지가 없습니다.")
        return
    
    # 결과 저장용 변수
    results = []
    successful = 0
    skipped = 0
    failed = 0
    
    # 시작 시간
    start_time = time.time()
    
    # 파일 업로드
    for file_path in tqdm(files, desc="업로드 중"):
        # 상대 경로 계산
        rel_path = os.path.relpath(file_path, config['source_dir'])
        
        # 저장 경로 결정 (폴더 구조 무조건 유지)
        dest_path = os.path.join(config['destination_prefix'], rel_path)
        
        # 경로 구분자 정규화 (윈도우에서도 작동하도록)
        dest_path = dest_path.replace('\\', '/')
        
        # 업로드
        result = upload_file(bucket, file_path, dest_path, config['skip_existing'])
        results.append(result)
        
        # 결과 카운팅
        if result['success']:
            if result.get('skipped'):
                skipped += 1
            else:
                successful += 1
        else:
            failed += 1
            print(f"실패: {file_path} -> {result.get('error', '알 수 없는 오류')}")
    
    # 소요 시간
    elapsed_time = time.time() - start_time
    
    # 결과 출력
    print("\n===== 업로드 결과 =====")
    print(f"총 파일: {len(files)}개")
    print(f"성공: {successful}개")
    print(f"건너뜀 (이미 존재): {skipped}개")
    print(f"실패: {failed}개")
    print(f"총 소요 시간: {elapsed_time:.2f}초")
    
    # 결과를 JSON 파일로 저장
    result_json = {
        'timestamp': time.strftime('%Y-%m-%d %H:%M:%S'),
        'total': len(files),
        'successful': successful,
        'skipped': skipped,
        'failed': failed,
        'results': results
    }
    
    with open('upload_results.json', 'w', encoding='utf-8') as f:
        json.dump(result_json, f, ensure_ascii=False, indent=2)
    
    print("결과가 upload_results.json 파일에 저장되었습니다.")
    
    # URL 매핑 파일 생성 (웹앱 연동용)
    url_mapping = {}
    for result in results:
        if result['success'] and 'url' in result:
            # 상대 경로를 키로 사용 (폴더 정보 포함)
            rel_path = result['path'].replace(f"{config['destination_prefix']}/", '')
            url_mapping[rel_path] = result['url']
    
    with open('firebase_url_mapping.json', 'w', encoding='utf-8') as f:
        json.dump(url_mapping, f, ensure_ascii=False, indent=2)
    
    print("URL 매핑이 firebase_url_mapping.json 파일에 저장되었습니다.")
    print("이 파일을 사용하여 웹앱의 이미지 URL을 Firebase로 쉽게 변경할 수 있습니다.")

if __name__ == "__main__":
    main()