from typing import Iterator, Tuple, List, Dict, Union, Any, Iterable
import torch
from torch.utils.data import IterableDataset, DataLoader, ChainDataset, get_worker_info
from torch.utils.data.dataset import T_co, Dataset
from transformers import PreTrainedTokenizer, BatchEncoding, AutoTokenizer
import ijson
import numpy as np
from sklearn.model_selection import train_test_split
from sklearn.preprocessing import MultiLabelBinarizer
from skmultilearn.model_selection import IterativeStratification
from abc import ABC, abstractmethod
import itertools
from torch.utils.data._utils.collate import default_collate
from collections import defaultdict
from strategies import BatchingStrategy
import random

random.seed(42)


class AbstractMultiTaskDataset(ABC, IterableDataset):
    def __init__(self, task_name: str, json_file: str, tokenizer: PreTrainedTokenizer, fields: List[str],
                 sample_size, ctrl_token: str, max_len: int, block_size=100):
        self.task_name = task_name
        self.data_file = json_file
        self.tokenizer = tokenizer
        self.fields = fields
        self.sample_size = sample_size
        self.ctrl_token = ctrl_token
        self.max_len = max_len
        self.block_size = block_size

    @abstractmethod
    def sub_sample(self, json_parse: List[Dict]) -> Iterator:
        pass

    @abstractmethod
    def preprocess(self, line: Dict[str, str]) -> Union[
        Tuple[str, BatchEncoding, torch.Tensor], List[Tuple[str, List[BatchEncoding]]]]:
        pass

    def iter_slice(self, curr_iter, worker_info):
        for idx, data_instance in enumerate(curr_iter):
            if not worker_info or (idx // self.block_size) % worker_info.num_workers == worker_info.id:
                yield data_instance

    def __iter__(self) -> Iterator[T_co]:
        # data is assumed to be a json file
        try:
            file_iter = open(self.data_file, "rb")
            json_parse = ijson.items(file_iter, 'item')
            peek = next(json_parse)
            json_parse = itertools.chain([peek], json_parse)
        except:
            file_iter = open(self.data_file, "rb")
            json_parse = ijson.items(file_iter, '', multiple_values=True)

        if self.sample_size == -1:
            map_itr = map(self.preprocess, json_parse)
        else:
            map_itr = map(self.preprocess, self.sub_sample(list(json_parse)))

        worker_info = get_worker_info()
        if worker_info:
            map_itr = self.iter_slice(map_itr, worker_info)
        return map_itr

    def tokenized_input(self, input_map: Dict[str, str]) -> BatchEncoding:
        text = "" if not self.ctrl_token else self.ctrl_token
        for field in self.fields:
            if input_map[field]:
                text += " " + input_map[field]
        input_ids = self.tokenizer(text, padding="max_length", truncation=True, return_tensors="pt",
                                   max_length=self.max_len)
        return input_ids["input_ids"].flatten()


class ClassificationDataset(AbstractMultiTaskDataset):
    def __init__(self, task_name: str, json_file: str, tokenizer: PreTrainedTokenizer, fields: List[str],
                 label_field: str, labels: Dict[str, int], sample_size=-1, ctrl_token: str = None, max_len: int = 512):
        super().__init__(task_name, json_file, tokenizer, fields, sample_size, ctrl_token, max_len)
        self.labels = labels
        self.label_field = label_field

    def preprocess(self, line: Dict[str, str]) -> Tuple[str, BatchEncoding, int]:
        # Splits the line into text and label and applies preprocessing to the text
        label = line[self.label_field]
        input_ids = self.tokenized_input(line)
        return self.task_name, input_ids, self.labels[label]

    def sub_sample(self, json_parse: List[Dict]) -> Iterator:
        X_ids = np.array([d["corpus_id"] for d in json_parse])
        y = np.array([self.labels[d[self.label_field]] for d in json_parse])
        ids, _, _, _ = train_test_split(X_ids, y, train_size=self.sample_size, random_state=42,
                                        stratify=y)
        X = [d for d in json_parse if d["corpus_id"] in ids]
        return X


class MultiLabelClassificationDataset(ClassificationDataset):
    def __init__(self, task_name: str, json_file: str, tokenizer: PreTrainedTokenizer, fields: List[str],
                 label_field: str, labels: Dict[str, int], sample_size=-1, ctrl_token: str = None, max_len: int = 512):
        super().__init__(task_name, json_file, tokenizer, fields, label_field, labels, sample_size, ctrl_token, max_len)
        self.labels = dict(sorted(labels.items()))
        self.mlb = MultiLabelBinarizer()
        self.mlb.fit([list(self.labels.keys())])

    def preprocess(self, line: Dict[str, str]) -> Tuple[str, BatchEncoding, np.ndarray]:
        label = line[self.label_field]
        input_ids = self.tokenized_input(line)
        return self.task_name, input_ids, self.mlb.transform([label]).flatten().astype(float)

    def sub_sample(self, json_parse: List[Dict]) -> Iterator:
        X_ids = np.array([d["corpus_id"] for d in json_parse])
        y = self.mlb.transform([tuple(d[self.label_field]) for d in json_parse])
        sub_sample_ratio = self.sample_size / len(json_parse)
        stratifier = IterativeStratification(n_splits=2, order=1,
                                             sample_distribution_per_fold=[sub_sample_ratio,
                                                                           1 - sub_sample_ratio, ])
        _, indices = next(stratifier.split(X_ids, y))
        ids = X_ids[indices]
        X = [d for d in json_parse if d["corpus_id"] in ids]
        return X


class TripletDataset(AbstractMultiTaskDataset):
    def __init__(self, task_name: str, json_file: str, tokenizer: PreTrainedTokenizer, fields: List[str],
                 sample_size=-1, ctrl_token: str = None, max_len: int = 512):
        super().__init__(task_name, json_file, tokenizer, fields, sample_size, ctrl_token, max_len)

    def preprocess(self, line: Dict[str, str]) -> List[Tuple[str, List[BatchEncoding]]]:
        # Splits the line into text and label and applies preprocessing to the text
        query, candidates = line["query"], line["candidates"]
        pos_candidates, neg_candidates = [c for c in candidates if c["score"]], [c for c in candidates if
                                                                                 not c["score"]]
        tokenized_input_list = []
        tokenized_query = self.tokenized_input(query)
        for pos in pos_candidates:
            neg = None
            if neg_candidates:
                neg = neg_candidates.pop()
            if neg:
                tokenized_pos = self.tokenized_input(pos)
                tokenized_neg = self.tokenized_input(neg)
                tokenized_input_list.append((self.task_name, [tokenized_query, tokenized_pos, tokenized_neg]))
        return tokenized_input_list

    def sub_sample(self, json_parse: List[Dict]) -> Iterator:
        return json_parse[:self.sample_size // 5]

    def __iter__(self):
        return itertools.chain(*super().__iter__())


class CustomChainDataset(ChainDataset):
    def __init__(self, datasets: Iterable[Dataset], batch_size, batching_strategy=BatchingStrategy.TASK_PER_BATCH):
        super().__init__(datasets)
        self.batch_size = batch_size
        self.batching = batching_strategy

    def __iter__(self):
        if self.batching == BatchingStrategy.MIXED_RANDOM:
            iters = list(map(iter, self.datasets))
            while iters:
                it = random.choice(iters)
                try:
                    yield next(it)
                except StopIteration:
                    iters.remove(it)
        elif self.batching == BatchingStrategy.TASK_PER_BATCH:
            iters = list(map(iter, self.datasets))
            di = 0
            while iters:
                it = iters[di]
                i = -1
                for x in it:
                    i += 1
                    if i < self.batch_size:
                        yield x
                    else:
                        di = (di + 1) % len(iters)
                        break
                if i == -1:
                    iters.remove(it)
                    if di >= len(iters):
                        di = 0
        else:
            yield from super().__iter__()


def multi_collate(batch: List[Any]) -> Dict[str, List[Any]]:
    task_sub_batch = defaultdict(list)
    for b in batch:
        task_sub_batch[b[0]].append(b[1:])
    return {task: default_collate(sub_batch) for task, sub_batch in task_sub_batch.items()}


if __name__ == '__main__':
    tokenizer = AutoTokenizer.from_pretrained("allenai/specter")
    with open("sample_data/mesh_descriptors.txt", "r") as f:
        labels = f.readlines()
    labels = {l.strip(): i for i, l in enumerate(labels)}
    cls_dataset = ClassificationDataset(task_name="mesh", json_file="sample_data/mesh_small.json", tokenizer=tokenizer,
                                        fields=["title", "abstract"],
                                        label_field="descriptor", labels=labels, sample_size=100)
    trip_dataset = TripletDataset(task_name="s2and", json_file="sample_data/s2and_small.json",
                                  tokenizer=tokenizer,
                                  fields=["title", "abstract"], sample_size=100)
    with open("sample_data/fos_labels.txt", "r") as f:
        mlc_labels = f.readlines()
    mlc_labels = {l.strip(): i for i, l in enumerate(mlc_labels)}

    ml_cls_dataset = MultiLabelClassificationDataset(task_name="fos", json_file="sample_data/fos_small.json",
                                                     tokenizer=tokenizer,
                                                     fields=["title", "abstract"],
                                                     label_field="labels_text", labels=mlc_labels, sample_size=100)
    multi_dataset = CustomChainDataset([cls_dataset, trip_dataset, ml_cls_dataset], batch_size=32)
    dataloader = DataLoader(multi_dataset, batch_size=32, collate_fn=multi_collate, num_workers=4)
    for i, data in enumerate(dataloader):
        print(i)
        for task, batch in data.items():
            print(task, len(batch[0]))
