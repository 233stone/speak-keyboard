#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FunASR模型下载脚本
并行下载所有模型文件
"""

import sys
import json
import threading
from funasr_config import MODEL_REVISION, get_models_for_download

def download_model(model_config, progress_callback=None):
    """下载单个模型"""
    model_name = model_config["name"]
    model_type = model_config["type"]
    
    try:
        from funasr import AutoModel
        
        if progress_callback:
            progress_callback(model_type, "downloading", 0)
        
        # 下载模型
        AutoModel(
            model=model_name,
            model_revision=MODEL_REVISION
        )
        
        if progress_callback:
            progress_callback(model_type, "completed", 100)
            
        return {"success": True, "model": model_type}
        
    except Exception as e:
        if progress_callback:
            progress_callback(model_type, "error", 0, str(e))
        return {"success": False, "model": model_type, "error": str(e)}

def main():
    """主函数：并行下载所有模型"""
    
    # 从统一配置获取模型列表
    models = get_models_for_download()
    
    # 进度跟踪
    progress = {"asr": 0, "vad": 0, "punc": 0}
    results = {}
    completed_count = 0
    total_count = len(models)
    count_lock = threading.Lock()  # 添加锁保护计数器
    results_lock = threading.Lock()
    
    def progress_callback(model_type, stage, percent, error=None):
        nonlocal completed_count
        
        # 使用锁保护共享变量的修改
        with count_lock:
            if stage == "downloading":
                progress[model_type] = percent
            elif stage == "completed":
                progress[model_type] = 100
                completed_count += 1
            elif stage == "error":
                progress[model_type] = 0
                completed_count += 1
            
            # 计算总体进度
            overall_progress = sum(progress.values()) / total_count
            current_completed = completed_count
        
        # 输出进度信息（在锁外执行I/O操作）
        status = {
            "stage": stage,
            "model": model_type,
            "progress": percent,
            "overall_progress": round(overall_progress, 1),
            "completed": current_completed,
            "total": total_count
        }
        
        if error:
            status["error"] = error
            
        print(json.dumps(status, ensure_ascii=False))
        sys.stdout.flush()
    
    # 启动并行下载线程
    threads = []
    for model_config in models:
        def worker(config=model_config):
            result = download_model(config, progress_callback)
            with results_lock:
                results[config["type"]] = result

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        threads.append(thread)
    
    # 等待所有线程完成
    for thread in threads:
        thread.join()
    
    # 检查结果
    failed_models = [model_type for model_type, result in results.items() if not result["success"]]
    
    if failed_models:
        final_result = {
            "success": False,
            "error": f"以下模型下载失败: {', '.join(failed_models)}",
            "failed_models": failed_models,
            "results": results
        }
    else:
        final_result = {
            "success": True,
            "message": "所有模型下载完成",
            "results": results
        }
    
    print(json.dumps(final_result, ensure_ascii=False))
    sys.stdout.flush()

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        error_result = {
            "success": False,
            "error": str(e)
        }
        print(json.dumps(error_result, ensure_ascii=False))
        sys.exit(1)