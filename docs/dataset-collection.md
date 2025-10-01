# 数据集收集功能

## 概述

这个功能可以自动保存每次语音转录的音频文件和转录文本，用于后续的模型微调。所有数据保存在 JSONL 格式中，方便后续处理和训练。

## 使用方法

### 启用数据集收集

在启动程序时添加 `--save-dataset` 参数：

```bash
python main.py --save-dataset
```

### 自定义保存目录

默认保存在 `dataset/` 目录，可以通过 `--dataset-dir` 参数自定义：

```bash
python main.py --save-dataset --dataset-dir my_training_data
```

## 数据格式

### 目录结构

```
dataset/
├── audio/                          # 音频文件目录
│   ├── 20250101_120000_123456-abcd1234.wav
│   ├── 20250101_120105_654321-efgh5678.wav
│   └── ...
└── dataset.jsonl                   # 元数据文件（每行一个 JSON）
```

### JSONL 格式

每行一条记录，包含以下字段：

```json
{
  "id": "20250101_120000_123456-abcd1234",
  "audio": "audio/20250101_120000_123456-abcd1234.wav",
  "text": "转录后的文本",
  "raw_text": "原始转录文本（未处理）",
  "duration": 3.5,
  "sample_rate": 16000,
  "inference_latency": 0.82,
  "confidence": 0.95,
  "timestamp": "2025-01-01T12:00:00Z"
}
```

**字段说明：**
- `id`: 唯一标识符（时间戳+UUID）
- `audio`: 音频文件相对路径
- `text`: 转录后的文本（经过后处理）
- `raw_text`: 原始转录文本（未经后处理）
- `duration`: 音频时长（秒）
- `sample_rate`: 采样率
- `inference_latency`: 推理耗时（秒）
- `confidence`: 置信度（0-1）
- `timestamp`: 转录时间戳（UTC）

## 数据处理示例

### 读取数据集

```python
import json
from pathlib import Path

dataset_dir = Path("dataset")
jsonl_path = dataset_dir / "dataset.jsonl"

# 读取所有记录
records = []
with open(jsonl_path, "r", encoding="utf-8") as f:
    for line in f:
        records.append(json.loads(line))

print(f"共有 {len(records)} 条记录")

# 读取音频和文本
for record in records:
    audio_path = dataset_dir / record["audio"]
    text = record["text"]
    print(f"音频: {audio_path}, 文本: {text}")
```

### 转换为训练格式

```python
import json
from pathlib import Path

def convert_to_training_format(dataset_dir: str, output_file: str):
    """将数据集转换为常见的训练格式"""
    dataset_dir = Path(dataset_dir)
    jsonl_path = dataset_dir / "dataset.jsonl"
    
    training_data = []
    with open(jsonl_path, "r", encoding="utf-8") as f:
        for line in f:
            record = json.loads(line)
            # 转换为绝对路径
            audio_path = (dataset_dir / record["audio"]).absolute()
            training_data.append({
                "audio_filepath": str(audio_path),
                "text": record["text"],
                "duration": record["duration"],
            })
    
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(training_data, f, ensure_ascii=False, indent=2)
    
    print(f"已转换 {len(training_data)} 条数据到 {output_file}")

# 使用示例
convert_to_training_format("dataset", "training_manifest.json")
```

### 过滤数据

```python
import json
from pathlib import Path
import shutil

def filter_dataset(
    src_dir: str,
    dst_dir: str,
    min_duration: float = 0.5,
    max_duration: float = 30.0,
    min_confidence: float = 0.8
):
    """过滤掉不符合条件的数据"""
    src_dir = Path(src_dir)
    dst_dir = Path(dst_dir)
    dst_audio_dir = dst_dir / "audio"
    dst_audio_dir.mkdir(parents=True, exist_ok=True)
    
    src_jsonl = src_dir / "dataset.jsonl"
    dst_jsonl = dst_dir / "dataset.jsonl"
    
    filtered_count = 0
    total_count = 0
    
    with open(src_jsonl, "r", encoding="utf-8") as f_in:
        with open(dst_jsonl, "w", encoding="utf-8") as f_out:
            for line in f_in:
                total_count += 1
                record = json.loads(line)
                
                # 应用过滤条件
                if (min_duration <= record["duration"] <= max_duration and
                    record["confidence"] >= min_confidence and
                    len(record["text"].strip()) > 0):
                    
                    # 复制音频文件
                    src_audio = src_dir / record["audio"]
                    dst_audio = dst_dir / record["audio"]
                    shutil.copy2(src_audio, dst_audio)
                    
                    # 写入记录
                    f_out.write(line)
                    filtered_count += 1
    
    print(f"过滤完成: {filtered_count}/{total_count} 条数据符合条件")

# 使用示例
filter_dataset(
    "dataset",
    "dataset_filtered",
    min_duration=1.0,      # 至少 1 秒
    max_duration=20.0,     # 最多 20 秒
    min_confidence=0.85    # 置信度至少 0.85
)
```

## 注意事项

1. **存储空间**：音频文件会占用大量空间，建议定期清理或备份
2. **隐私保护**：录音可能包含敏感信息，请妥善保管数据
3. **数据质量**：建议定期检查转录质量，过滤掉低质量样本
4. **原子性**：音频文件写入使用原子操作，确保数据完整性
5. **错误处理**：数据保存失败不会影响正常的转录输出

## 微调建议

1. **数据清洗**：过滤掉置信度低、时长异常的样本
2. **数据增强**：可以对音频进行噪声添加、速度变化等增强
3. **数据平衡**：确保不同场景、说话人的数据分布均匀
4. **标注检查**：人工检查一部分样本的转录准确性
5. **分割数据集**：划分训练集、验证集、测试集（如 80%/10%/10%）

## 示例工作流

```bash
# 1. 启动数据收集
python main.py --save-dataset --dataset-dir my_data

# 2. 使用一段时间后，查看收集的数据量
python -c "
import json
with open('my_data/dataset.jsonl') as f:
    count = sum(1 for _ in f)
print(f'共收集 {count} 条样本')
"

# 3. 过滤和清洗数据（使用上面的脚本）
python filter_dataset.py

# 4. 转换为训练格式
python convert_to_training_format.py

# 5. 开始微调模型
# （具体步骤取决于你使用的模型框架）
```

## 常见问题

**Q: 数据会自动去重吗？**
A: 不会。每次转录都会生成唯一的 ID，建议自己在后处理时根据音频内容进行去重。

**Q: 可以暂停收集吗？**
A: 可以。不使用 `--save-dataset` 参数启动即可。已收集的数据会保留。

**Q: 数据格式可以自定义吗？**
A: 可以修改 `app/plugins/dataset_recorder.py` 中的 `record` 字典来调整保存的字段。

**Q: 音频格式是什么？**
A: WAV 格式，16-bit PCM，采样率由配置文件决定（默认 16000Hz）。

