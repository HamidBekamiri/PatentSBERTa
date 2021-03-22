#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Citation: https://arxiv.org/abs/2010.08240

"""

from torch.utils.data import DataLoader
from sentence_transformers import models, losses, util
from sentence_transformers import SentencesDataset, LoggingHandler, SentenceTransformer
from sentence_transformers.cross_encoder import CrossEncoder
from sentence_transformers.cross_encoder.evaluation import CECorrelationEvaluator
from sentence_transformers.evaluation import EmbeddingSimilarityEvaluator
from sentence_transformers.readers import InputExample
from datetime import datetime
import logging
import csv
import torch
import tqdm
import sys
import math
import gzip
import os
import pandas as pd

#### Just some code to print debug information to stdout
logging.basicConfig(format='%(asctime)s - %(message)s',
                    datefmt='%Y-%m-%d %H:%M:%S',
                    level=logging.INFO,
                    handlers=[LoggingHandler()])
#### /print debug information to stdout


#You can specify any huggingface/transformers pre-trained model here, for example, bert-base-uncased, roberta-base, xlm-roberta-base
model_name = 'roberta-base'
top_k = int(sys.argv[2]) if len(sys.argv) > 2 else 3

batch_size = 16
num_epochs = 1
max_seq_length = 512

###### Read Datasets ######

#Check if dataset exsist. If not, download and extract  it
sts_dataset_path = 'datasets/stsbenchmark.tsv.gz'

if not os.path.exists(sts_dataset_path):
    util.http_get('https://sbert.net/datasets/stsbenchmark.tsv.gz', sts_dataset_path)

cross_encoder_path = '/home/ubuntu/deeppatentsimilarity/output/cross-encoder/stsb_indomain_'+model_name.replace("/", "-")+'-'+datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
bi_encoder_path = '/home/ubuntu/deeppatentsimilarity/output/bi-encoder/stsb_augsbert_SS_'+model_name.replace("/", "-")+'-'+datetime.now().strftime("%Y-%m-%d_%H-%M-%S")

###### Cross-encoder (simpletransformers) ######
logging.info("Loading cross-encoder model: {}".format(model_name))
# Use Huggingface/transformers model (like BERT, RoBERTa, XLNet, XLM-R) for cross-encoder model
cross_encoder = CrossEncoder(model_name, num_labels=1)


###### Bi-encoder (sentence-transformers) ######
logging.info("Loading bi-encoder model: {}".format(model_name))
# Use Huggingface/transformers model (like BERT, RoBERTa, XLNet, XLM-R) for mapping tokens to embeddings
word_embedding_model = models.Transformer(model_name, max_seq_length=max_seq_length)

# Apply mean pooling to get one fixed sized sentence vector
pooling_model = models.Pooling(word_embedding_model.get_word_embedding_dimension(),
                               pooling_mode_mean_tokens=True,
                               pooling_mode_cls_token=False,
                               pooling_mode_max_tokens=False)

bi_encoder = SentenceTransformer(modules=[word_embedding_model, pooling_model])


#####################################################
#
# Step 1: Train cross-encoder model with STSbenchmark
#
#####################################################

logging.info("Step 1: Train cross-encoder: {} with STSbenchmark (gold dataset)".format(model_name))

gold_samples = []
dev_samples = []
test_samples = []

with gzip.open(sts_dataset_path, 'rt', encoding='utf8') as fIn:
    reader = csv.DictReader(fIn, delimiter='\t', quoting=csv.QUOTE_NONE)
    for row in reader:
        score = float(row['score']) / 5.0  # Normalize score to range 0 ... 1

        if row['split'] == 'dev':
            dev_samples.append(InputExample(texts=[row['sentence1'], row['sentence2']], label=score))
        elif row['split'] == 'test':
            test_samples.append(InputExample(texts=[row['sentence1'], row['sentence2']], label=score))
        else:
            #As we want to get symmetric scores, i.e. CrossEncoder(A,B) = CrossEncoder(B,A), we pass both combinations to the train set
            gold_samples.append(InputExample(texts=[row['sentence1'], row['sentence2']], label=score))
            gold_samples.append(InputExample(texts=[row['sentence2'], row['sentence1']], label=score))


# We wrap gold_samples (which is a List[InputExample]) into a pytorch DataLoader
train_dataloader = DataLoader(gold_samples, shuffle=True, batch_size=batch_size)


# We add an evaluator, which evaluates the performance during training
evaluator = CECorrelationEvaluator.from_input_examples(dev_samples, name='sts-dev')

# Configure the training
warmup_steps = math.ceil(len(train_dataloader) * num_epochs * 0.1) #10% of train data for warm-up
logging.info("Warmup-steps: {}".format(warmup_steps))

# Train the cross-encoder model
cross_encoder.fit(train_dataloader=train_dataloader,
          evaluator=evaluator,
          epochs=num_epochs,
          evaluation_steps=1000,
          warmup_steps=warmup_steps,
          output_path=cross_encoder_path)

############################################################################
#
# Step 2: Find silver pairs to label
#
############################################################################

#### Top k similar sentences to be retrieved ####
#### Larger the k, bigger the silver dataset ####

logging.info("Step 2.1: Generate STSbenchmark (silver dataset) using pretrained SBERT \
    model and top-{} semantic search combinations".format(top_k))

silver_data = []
# df_claim_oneclaim = pd.read_csv ('/home/ubuntu/deeppatentsimilarity/patentdata/df_claims_oneclaim_500.csv', encoding="ISO-8859-1")
df_claim_oneclaim = pd.read_csv ('/home/ubuntu/deeppatentsimilarity/patentdata/df_claims_oneclaim_1000.csv', encoding="ISO-8859-1")
df_claim_oneclaim['text'] = df_claim_oneclaim['text'].str[3:]
sentences = df_claim_oneclaim.text.to_list()
sent2idx = dict(zip(df_claim_oneclaim.text, df_claim_oneclaim.patent_id))

# duplicates  = pd.read_csv ('/home/ubuntu/deeppatentsimilarity/patentdata/duplicates_4.csv', encoding="ISO-8859-1")

# for sample in gold_samples:
#     sentences.update(sample.texts)

# sentences = list(sentences) # unique sentences
# sent2idx = {sentence: idx for idx, sentence in enumerate(sentences)} # storing id and sentence in dictionary
# duplicates = set((sent2idx[data.texts[0]], sent2idx[data.texts[1]]) for data in gold_samples) # not to include gold pairs of sentences again


# For simplicity we use a pretrained model
semantic_model_name = 'roberta-base-nli-stsb-mean-tokens'
semantic_search_model = SentenceTransformer(semantic_model_name)
logging.info("Encoding unique sentences with semantic search model: {}".format(semantic_model_name))

# encoding all unique sentences present in the training dataset
embeddings = semantic_search_model.encode(sentences, batch_size=batch_size, convert_to_tensor=True)

logging.info("Retrieve top-{} with semantic search model: {}".format(top_k, semantic_model_name))

# retrieving top-k sentences given a sentence from the dataset
progress = tqdm.tqdm(unit="docs", total=len(sent2idx))
for idx in range(len(str(sentences))):
    sentence_embedding = embeddings[idx]
    cos_scores = util.pytorch_cos_sim(sentence_embedding, embeddings)[0]
    cos_scores = cos_scores.cpu()
    progress.update(1)

    #We use torch.topk to find the highest 5 scores
    top_results = torch.topk(cos_scores, k=top_k+1)
    
    for score, iid in zip(top_results[0], top_results[1]):
#         if iid != idx and (iid, idx) not in duplicates:
        if iid != idx:
            silver_data.append((sentences[idx], sentences[iid]))
#             duplicates.add((idx,iid))

progress.reset()
progress.close()

logging.info("Length of silver_dataset generated: {}".format(len(silver_data)))
logging.info("Step 2.2: Label STSbenchmark (silver dataset) with cross-encoder: {}".format(model_name))
cross_encoder = CrossEncoder(cross_encoder_path)
silver_scores = cross_encoder.predict(silver_data)

# All model predictions should be between [0,1]
assert all(0.0 <= score <= 1.0 for score in silver_scores)

############################################################################################
#
# Step 3: Train bi-encoder model with both STSbenchmark and labeled AllNlI - Augmented SBERT
#
############################################################################################

logging.info("Step 3: Train bi-encoder: {} with STSbenchmark (gold + silver dataset)".format(model_name))

# Convert the dataset to a DataLoader ready for training
logging.info("Read STSbenchmark gold and silver train dataset")
silver_samples = list(InputExample(texts=[data[0], data[1]], label=score) for \
    data, score in zip(silver_data, silver_scores))

train_dataset = SentencesDataset(gold_samples + silver_samples, bi_encoder)
train_dataloader = DataLoader(train_dataset, shuffle=True, batch_size=batch_size)
train_loss = losses.CosineSimilarityLoss(model=bi_encoder)

logging.info("Read STSbenchmark dev dataset")
evaluator = EmbeddingSimilarityEvaluator.from_input_examples(dev_samples, name='sts-dev')

# Configure the training.
warmup_steps = math.ceil(len(train_dataset) * num_epochs / batch_size * 0.1) #10% of train data for warm-up
logging.info("Warmup-steps: {}".format(warmup_steps))

# Train the bi-encoder model
bi_encoder.fit(train_objectives=[(train_dataloader, train_loss)],
          evaluator=evaluator,
          epochs=num_epochs,
          evaluation_steps=1000,
          warmup_steps=warmup_steps,
          output_path=bi_encoder_path
          )

#################################################################################
#
# Evaluate cross-encoder and Augmented SBERT performance on STS benchmark dataset
#
#################################################################################

# load the stored augmented-sbert model
bi_encoder = SentenceTransformer(bi_encoder_path)
test_evaluator = EmbeddingSimilarityEvaluator.from_input_examples(test_samples, name='sts-test')
test_evaluator(bi_encoder, output_path=bi_encoder_path)