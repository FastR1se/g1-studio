"""Названия объектов: классы COCO (их знает YOLO) и русские имена."""
from __future__ import annotations

COCO_NAMES = [
    "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train", "truck", "boat", "traffic light",
    "fire hydrant", "stop sign", "parking meter", "bench", "bird", "cat", "dog", "horse", "sheep", "cow",
    "elephant", "bear", "zebra", "giraffe", "backpack", "umbrella", "handbag", "tie", "suitcase", "frisbee",
    "skis", "snowboard", "sports ball", "kite", "baseball bat", "baseball glove", "skateboard", "surfboard",
    "tennis racket", "bottle", "wine glass", "cup", "fork", "knife", "spoon", "bowl", "banana", "apple",
    "sandwich", "orange", "broccoli", "carrot", "hot dog", "pizza", "donut", "cake", "chair", "couch",
    "potted plant", "bed", "dining table", "toilet", "tv", "laptop", "mouse", "remote", "keyboard",
    "cell phone", "microwave", "oven", "toaster", "sink", "refrigerator", "book", "clock", "vase",
    "scissors", "teddy bear", "hair drier", "toothbrush",
]

# "ball" — красный мяч (ищется по цвету), остальное — классы YOLO
TARGETS = ["ball"] + COCO_NAMES

NAMES_RU = {
    "ball": "мяч", "person": "человек", "chair": "стул", "couch": "диван", "potted plant": "растение",
    "bottle": "бутылка", "cup": "кружка", "bowl": "миска", "banana": "банан", "apple": "яблоко",
    "orange": "апельсин", "suitcase": "чемодан", "backpack": "рюкзак", "handbag": "сумка", "laptop": "ноутбук",
    "tv": "телевизор", "vase": "ваза", "clock": "часы", "book": "книга", "scissors": "ножницы",
    "teddy bear": "плюшевый мишка", "dining table": "стол", "bench": "скамейка", "bed": "кровать",
    "sports ball": "мяч", "dog": "собака", "cat": "кошка", "bicycle": "велосипед", "car": "машина",
    "cell phone": "телефон", "keyboard": "клавиатура", "mouse": "мышь", "refrigerator": "холодильник",
    "microwave": "микроволновка", "sink": "раковина", "toilet": "унитаз", "umbrella": "зонт",
    "wine glass": "бокал", "fork": "вилка", "knife": "нож", "spoon": "ложка", "remote": "пульт",
}

# Если модель вернёт цель по-русски — переведём сами
TARGET_RU = {
    "мяч": "ball", "мячик": "ball", "шар": "ball", "человек": "person", "человека": "person", "люди": "person",
    "стул": "chair", "кресло": "chair", "диван": "couch", "растение": "potted plant", "цветок": "potted plant",
    "бутылка": "bottle", "бутылку": "bottle", "кружка": "cup", "кружку": "cup", "чашка": "cup", "чашку": "cup",
    "стакан": "cup", "миска": "bowl", "миску": "bowl", "тарелка": "bowl", "банан": "banana", "яблоко": "apple",
    "апельсин": "orange", "чемодан": "suitcase", "рюкзак": "backpack", "сумка": "handbag", "ноутбук": "laptop",
    "телевизор": "tv", "ваза": "vase", "вазу": "vase", "часы": "clock", "книга": "book", "книгу": "book",
    "ножницы": "scissors", "стол": "dining table", "скамейка": "bench", "собака": "dog", "кошка": "cat",
}


def name_ru(label: str) -> str:
    return NAMES_RU.get(label, label)
