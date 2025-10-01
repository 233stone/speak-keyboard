#!/usr/bin/env python3
"""查看和统计数据集的工具脚本"""

import argparse
import json
from pathlib import Path
from typing import List, Dict


def load_dataset(dataset_dir: str) -> List[Dict]:
    """加载数据集"""
    dataset_path = Path(dataset_dir)
    jsonl_path = dataset_path / "dataset.jsonl"
    
    if not jsonl_path.exists():
        print(f"❌ 数据集不存在: {jsonl_path}")
        return []
    
    records = []
    with open(jsonl_path, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                records.append(json.loads(line))
    
    return records


def show_statistics(records: List[Dict]) -> None:
    """显示数据集统计信息"""
    if not records:
        print("数据集为空")
        return
    
    total_count = len(records)
    total_duration = sum(r["duration"] for r in records)
    avg_duration = total_duration / total_count
    avg_latency = sum(r["inference_latency"] for r in records) / total_count
    avg_confidence = sum(r.get("confidence", 0) for r in records) / total_count
    
    # 文本长度统计
    text_lengths = [len(r["text"]) for r in records]
    avg_text_length = sum(text_lengths) / total_count
    max_text_length = max(text_lengths)
    min_text_length = min(text_lengths)
    
    print("\n" + "="*60)
    print("📊 数据集统计")
    print("="*60)
    print(f"样本数量:    {total_count:>8} 条")
    print(f"总时长:      {total_duration:>8.1f} 秒 ({total_duration/60:.1f} 分钟)")
    print(f"平均时长:    {avg_duration:>8.2f} 秒")
    print(f"平均推理:    {avg_latency:>8.2f} 秒")
    print(f"平均置信度:  {avg_confidence:>8.2%}")
    print(f"平均文本长度: {avg_text_length:>7.1f} 字")
    print(f"文本长度范围: {min_text_length:>3} - {max_text_length:>3} 字")
    print("="*60 + "\n")


def show_samples(records: List[Dict], count: int = 5) -> None:
    """显示样本"""
    print(f"\n📝 最近 {min(count, len(records))} 条样本：\n")
    
    for i, record in enumerate(records[-count:], 1):
        print(f"[{i}] ID: {record['id']}")
        print(f"    文本: {record['text']}")
        print(f"    时长: {record['duration']:.2f}s | "
              f"置信度: {record.get('confidence', 0):.2%} | "
              f"推理: {record['inference_latency']:.2f}s")
        print(f"    时间: {record['timestamp']}")
        print()


def filter_low_quality(records: List[Dict], 
                       min_confidence: float = 0.8,
                       min_duration: float = 0.5) -> List[Dict]:
    """过滤低质量样本"""
    filtered = [
        r for r in records
        if r.get("confidence", 0) >= min_confidence and 
           r["duration"] >= min_duration and
           len(r["text"].strip()) > 0
    ]
    return filtered


def main():
    parser = argparse.ArgumentParser(description="查看数据集统计信息")
    parser.add_argument("--dataset-dir", default="dataset", help="数据集目录")
    parser.add_argument("--samples", type=int, default=5, help="显示最近N条样本")
    parser.add_argument("--filter", action="store_true", help="只显示高质量样本")
    parser.add_argument("--min-confidence", type=float, default=0.8, help="最小置信度")
    parser.add_argument("--min-duration", type=float, default=0.5, help="最小时长（秒）")
    
    args = parser.parse_args()
    
    # 加载数据集
    records = load_dataset(args.dataset_dir)
    
    if not records:
        return
    
    print(f"\n📁 数据集目录: {Path(args.dataset_dir).absolute()}")
    
    # 显示统计信息
    show_statistics(records)
    
    # 如果需要过滤
    if args.filter:
        filtered_records = filter_low_quality(
            records,
            min_confidence=args.min_confidence,
            min_duration=args.min_duration
        )
        print(f"\n🔍 过滤后样本数: {len(filtered_records)}/{len(records)} "
              f"({len(filtered_records)/len(records)*100:.1f}%)")
        print(f"   条件: 置信度 >= {args.min_confidence:.0%}, "
              f"时长 >= {args.min_duration:.1f}s")
        records = filtered_records
        
        if records:
            show_statistics(records)
    
    # 显示样本
    if args.samples > 0:
        show_samples(records, args.samples)


if __name__ == "__main__":
    main()

