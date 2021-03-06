# Copyright (c) 2018-present, Facebook, Inc.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.
#

import os, re, io, math
import subprocess
from collections import OrderedDict
from logging import getLogger
import numpy as np
import torch
from torch import nn

from .utils import restore_segmentation


logger = getLogger()


TOOLS_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'tools')
BLEU_SCRIPT_PATH = os.path.join(TOOLS_PATH, 'mosesdecoder/scripts/generic/multi-bleu-ch.perl')
assert os.path.isfile(BLEU_SCRIPT_PATH), "Moses not found. Please be sure you downloaded Moses in %s" % TOOLS_PATH

PADDING_IDX=1
MAX_SENT_LEN=75 #TODO: 75, 130

def get_data(input_sents, tokenizer, issanwen, dopmpad):
    sent_str = []
    positions = []
    sentences = []
    sentences_len = []
    sent_str_abs = []
    positions_abs = []
    sentences_abs = []
    sentences_abs_len = []
    length_in_count = np.zeros(int(MAX_SENT_LEN/10)+1)
    unk_words = {}
    line_count=0
    too_long_sent_count = 0
    long_sent_count = 0
    # for ind in range(len(input_sents)):
    for ind in range(len(input_sents)):
        sent=input_sents[ind]
        realmax_len = int(MAX_SENT_LEN)

        if len(sent) > realmax_len:
            # print("Long sentence with len %i in line %i." % (len(sent),line_count))
            sent=sent[0:realmax_len]
            sent = list(zng(sent)) # ends with punc
            if len(sent) == 0:
                sent=''
                too_long_sent_count+=1
            else:
                assert len(sent)==1
                sent = sent[0]
                long_sent_count+=1
        token_s = tokenizer.tokenize(sent)
        # if len(token_s) == 0:
        #     print("Empty sentence in line %i." % line_count)
        if len(token_s) > 21: #TODO: 31
            # index sentence words
            indexed = tokenizer.convert_tokens_to_ids(token_s)
            unk_idxs = [i for i, e in enumerate(indexed) if e == 100]
            for unk_idx in unk_idxs:
                w = sent[unk_idx] 
                unk_words[w] = unk_words.get(w, 0) + 1
            if dopmpad:
                while len(indexed) < 4*(7+1):
                    indexed.append(PADDING_IDX)
                indexed=indexed[0:4*(7+1)]
                ind_len = len(indexed)
                indexed = np.array(indexed)
                sliced = list(range(2,ind_len+1,2))+list(range(2,ind_len+1,2))
                # logger.info(ind)
                # logger.info(indexed.shape)
                indexed = np.insert(indexed, sliced, [PADDING_IDX]*ind_len)
                # logger.info(indexed.shape)
                # logger.info(indexed)

            # add sentence
            sent_str.append(sent)
            positions.append([len(sentences), len(sentences) + len(indexed)])
            sentences_len.append(len(indexed))
            sentences.extend(indexed)
            sentences.append(-1)

            if issanwen:
                summary = shorten_sents(sent, min_len=31, max_len=45)
                token_s_abs = tokenizer.tokenize(summary)
                indexed_abs = tokenizer.convert_tokens_to_ids(token_s_abs)
                sent_str_abs.append(summary)
                positions_abs.append([len(sentences_abs), len(sentences_abs) + len(indexed_abs)])
                sentences_abs_len.append(len(indexed_abs))
                sentences_abs.extend(indexed_abs)
                sentences_abs.append(-1)

            line_count+=1
            if len(token_s) > MAX_SENT_LEN:
                length_in_count[-1] += 1
            else:
                length_in_count[int(len(token_s)/10)] += 1
        # else:
        #     print("Short sentence in line %i. <=10" % line_count)


    # tensorize data
    positions = torch.LongTensor(positions)
    sentences = torch.LongTensor(sentences)
    positions_abs = torch.LongTensor(positions_abs)
    sentences_abs = torch.LongTensor(sentences_abs)
    data = {
        'dico': tokenizer,
        'positions': positions,
        'sentences': sentences,
        'positions_abs': positions_abs,
        'sentences_abs': sentences_abs,
        'unk_words': unk_words,
    }
    logger.info('long sentence count:')
    logger.info(long_sent_count)
    logger.info('long sentence that can not convert count:')
    logger.info(too_long_sent_count)
    length_in_count = length_in_count/np.sum(length_in_count)
    logger.info('sentence length bin count:')
    logger.info(length_in_count)
    logger.info('sentence length mean and std:')
    logger.info(np.mean(sentences_len))
    logger.info(np.std(sentences_len))
    if issanwen:
        logger.info('abstract sentence length mean and std:')
        logger.info(np.mean(sentences_abs_len))
        logger.info(np.std(sentences_abs_len))
    return data, sent_str, sent_str_abs

def batch_sentences_pm(sentences):
    """
    Take as input a list of n sentences (torch.LongTensor vectors) and return
    a tensor of size (s_len, n) where s_len is the length of the longest
    sentence, and a vector lengths containing the length of each sentence.
    """
    lengths = torch.LongTensor([len(s) + 2 for s in sentences])
    sent = torch.LongTensor(lengths.max(), lengths.size(0)).fill_(0)
    sent[0] = 4
    for i, s in enumerate(sentences):
        sent[1:lengths[i] - 1, i].copy_(s)
        sent[lengths[i] - 1, i] = 2
    return sent, lengths

class EvaluatorMT(object):

    def __init__(self, trainer, data, params):
        """
        Initialize evaluator.
        """
        self.encoder = trainer.encoder
        self.decoder = trainer.decoder
        self.discriminator = trainer.discriminator
        self.data = data
        self.dico = data['dico']
        self.params = params
        self.trainer = trainer
        self.blank_index = self.params.blank_index
        # self.init_bpe()

        # # create reference files for BLEU evaluation
        # self.create_reference_files()

    def init_bpe(self):
        """
        Index BPE words.
        """
        self.bpe_end = []
        for lang in self.params.langs:
            dico = self.data['dico'][lang]
            self.bpe_end.append(np.array([not dico[i].endswith('@@') for i in range(dico.__len__())]))

    def double_para(self, batch_ids,lens,lang_id, do_pad=False, do_bos=False, do_sep=False):
        batch_ids = batch_ids.clone()
        slen, bs = batch_ids.shape
        if do_pad or do_bos or do_sep:
            end_sent_2_idx = ((7+1)*1+7)*2-1 #29
            end_sent_4_idx = ((7+1)*3+7)*2-1 #61
            # batch_ids[end_sent_2_idx,:]=self.params.blank_index
            batch_ids[end_sent_4_idx,:]=self.params.blank_index
            batch_ids[end_sent_4_idx-3,:]=self.params.blank_index
            # logger.info(batch_ids[28:31,:])
            # logger.info(batch_ids[60:63,:])
        else:
            end_sent_2_idx = (7+1)*1+7
            end_sent_4_idx = (7+1)*3+7
            # batch_ids[end_sent_2_idx,:]=self.params.blank_index
            batch_ids[end_sent_4_idx-1:end_sent_4_idx+1,:]=self.params.blank_index
        # encode / decode / generate
        encoded = self.encoder(batch_ids, lens, lang_id)
        max_len = self.params.max_len[lang_id]
        batch_ids, lens, _ = self.decoder.generate(encoded, lang_id, max_len=max_len)
        return batch_ids,lens

    def eval_rythm(self, batch_ids, do_pad=False, do_bos=False, do_sep=False):
        correct=0
        ntcount=0
        batch_ids = batch_ids.cpu().detach().numpy()
        unk_index = self.params.unk_index
        pad_index = self.params.pad_index
        sos_index = self.params.sos_index
        sep_index = self.params.sep_index

        # logger.info('abc')
        # logger.info(batch_ids[0:12,0:12])
        # logger.info(batch_ids.shape)

        tmp_sents = []
        if do_pad:
            slen, bs = batch_ids.shape
            for i in range(bs):
                tmp_sent = batch_ids[:,i]
                tmp_sents.append(tmp_sent[tmp_sent!=pad_index])
            batch_ids = np.zeros([len(tmp_sents),len(max(tmp_sents,key = lambda x: len(x)))])
            for i,j in enumerate(tmp_sents):
                batch_ids[i][0:len(j)] = j
            batch_ids = batch_ids.T

        if do_bos:
            slen, bs = batch_ids.shape
            for i in range(bs):
                tmp_sent = batch_ids[:,i]
                tmp_sents.append(tmp_sent[tmp_sent!=sos_index])
            batch_ids = np.zeros([len(tmp_sents),len(max(tmp_sents,key = lambda x: len(x)))])
            for i,j in enumerate(tmp_sents):
                batch_ids[i][0:len(j)] = j
            batch_ids = batch_ids.T

        if do_sep:
            slen, bs = batch_ids.shape
            for i in range(bs):
                tmp_sent = batch_ids[:,i]
                tmp_sents.append(tmp_sent[tmp_sent!=sep_index])
            batch_ids = np.zeros([len(tmp_sents),len(max(tmp_sents,key = lambda x: len(x)))])
            for i,j in enumerate(tmp_sents):
                batch_ids[i][0:len(j)] = j
            batch_ids = batch_ids.T

        # logger.info(batch_ids[0:12,0:12])
        # logger.info(batch_ids.shape)

        if batch_ids.shape[0]>33:
            end_sent_2_toks = batch_ids[(7+1)*1+7,:]
            end_sent_4_toks = batch_ids[(7+1)*3+7,:]
            for idx in range(batch_ids.shape[1]):
                end_sent_2_tok = [end_sent_2_toks[idx]]
                end_sent_4_tok = [end_sent_4_toks[idx]]
                rythms_2 = self.dico['pm'].convert_ids_to_rythms(end_sent_2_tok)[0]
                rythms_4 = self.dico['pm'].convert_ids_to_rythms(end_sent_4_tok)[0]
                if (pad_index in rythms_2) or (pad_index in rythms_4) or (end_sent_2_tok==unk_index) or (end_sent_4_tok==unk_index):
                    ntcount += 1
                else:
                    tmp=0
                    for a_rhyme in rythms_2:
                        if a_rhyme in rythms_4:
                            tmp += 1
                            break
                    # if tmp==0:
                    #     print('not rythm: %s, %s' % (str(rythms_2),str(rythms_4)))
                    #     print(end_sent_2_tok)
                    #     print(end_sent_4_tok)
                    #     print(self.dico['pm'].convert_ids_to_tokens(batch_ids[:,idx]))
                    correct+=tmp

        else:
            correct += 0

        return correct, ntcount

    def get_pair_for_mono(self, lang):
        """
        Find a language pair for monolingual data.
        """
        candidates = [(l1, l2) for (l1, l2) in self.data['para'].keys() if l1 == lang or l2 == lang]
        assert len(candidates) > 0
        return sorted(candidates)[0]

    def mono_iterator(self, data_type, lang):
        """
        If we do not have monolingual validation / test sets, we take one from parallel data.
        """
        dataset = self.data['mono'][lang][data_type]
        if dataset is None:
            pair = self.get_pair_for_mono(lang)
            dataset = self.data['para'][pair][data_type]
            i = 0 if pair[0] == lang else 1
        else:
            i = None
        dataset.batch_size = 32
        for batch in dataset.get_iterator(shuffle=False, group_by_size=False, n_sentences=self.params.eval_length)(): #TODO: add para batch2
            yield batch if i is None else batch[i]

    def get_iterator(self, data_type, lang1, lang2):
        """
        Create a new iterator for a dataset.
        """
        assert data_type in ['valid', 'test', 'test1', 'test2']
        if lang2 is None or lang1 == lang2:
            for batch in self.mono_iterator(data_type, lang1):
                yield batch if lang2 is None else (batch, batch)
        else:
            k = (lang1, lang2) if lang1 < lang2 else (lang2, lang1)
            dataset = self.data['para'][k][data_type]
            dataset.batch_size = 32
            for batch in dataset.get_iterator(shuffle=False, group_by_size=False)():
                yield batch if lang1 < lang2 else batch[::-1]

    def create_reference_files(self):
        """
        Create reference files for BLEU evaluation.
        """
        params = self.params
        params.ref_paths = {}

        for (lang1, lang2), v in self.data['para'].items():

            assert lang1 < lang2
            lang1_id = params.lang2id[lang1]
            lang2_id = params.lang2id[lang2]

            for data_type in ['valid', 'test']:

                lang1_path = os.path.join(params.dump_path, 'ref.{0}-{1}.{2}.txt'.format(lang2, lang1, data_type))
                lang2_path = os.path.join(params.dump_path, 'ref.{0}-{1}.{2}.txt'.format(lang1, lang2, data_type))

                lang1_txt = []
                lang2_txt = []

                # convert to text
                for (sent1, len1), (sent2, len2) in self. ator(data_type, lang1, lang2):
                    lang1_txt.extend(convert_to_text(sent1, len1, self.dico[lang1], lang1_id, params))
                    lang2_txt.extend(convert_to_text(sent2, len2, self.dico[lang2], lang2_id, params))

                # replace <unk> by <<unk>> as these tokens cannot be counted in BLEU
                lang1_txt = [x.replace('<unk>', '<<unk>>') for x in lang1_txt]
                lang2_txt = [x.replace('<unk>', '<<unk>>') for x in lang2_txt]

                # export hypothesis
                with open(lang1_path, 'w', encoding='utf-8') as f:
                    f.write('\n'.join(lang1_txt) + '\n')
                with open(lang2_path, 'w', encoding='utf-8') as f:
                    f.write('\n'.join(lang2_txt) + '\n')

                # restore original segmentation
                restore_segmentation(lang1_path)
                restore_segmentation(lang2_path)

                # store data paths
                params.ref_paths[(lang2, lang1, data_type)] = lang1_path
                params.ref_paths[(lang1, lang2, data_type)] = lang2_path

    def eval_mono(self, lang, data_type, trainer, scores):
        logger.info("Evaluating Mono %s  (%s) ..." % (lang, data_type))
        assert data_type in ['valid', 'test1', 'test2']
        self.encoder.eval()
        self.decoder.eval()
        params = self.params
        lang_id = params.lang2id[lang]

        txt = []
        total = 0
        correct = 0

        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

        for batch in self.get_iterator(data_type, lang, None):
            # batch
            (x_gold, l_gold), (sent_abs, len_abs) = batch
            x_blank, l_blank = trainer.word_blank(x_gold, l_gold, lang_id)
            # if torch.cuda.is_available():
            #     words = words.cuda()
            x_gold, x_blank = x_gold.to(device), x_blank.to(device)
            encoded = self.encoder(x_blank, l_blank, lang_id)
            x_pred, l_pred, _ = self.decoder.generate(encoded, lang_id)
            total_, correct_ = self.get_blank_acc(x_gold, x_pred, x_blank)
            total += total_
            correct += correct_
            txt_blank = convert_to_text(x_blank, l_blank, self.dico[lang], lang_id, self.params, do_pad=False, do_bos=False, do_sep=False)
            txt_pred = convert_to_text(x_pred, l_pred, self.dico[lang], lang_id, self.params, do_pad=False, do_bos=False, do_sep=False)
            txt_all = []
            for i in range(len(txt_blank)):
                txt_all.append(txt_blank[i] + '\t###\t' + txt_pred[i])
            txt.extend(txt_all)
        # hypothesis / reference paths
        hyp_name = 'hyp{0}.{1}.{2}.txt'.format(scores['epoch'], lang, data_type)
        hyp_path = os.path.join(params.dump_path, hyp_name)

        # export sentences to hypothesis file / restore BPE segmentation
        with open(hyp_path, 'w', encoding='utf-8') as f:
            f.write('\n'.join(txt) + '\n')
        # restore_segmentation(hyp_path)

        # get acc
        logger.info("%s %s correct: %d" % (lang, data_type, int(correct)))
        logger.info("%s %s total  : %d" % (lang,data_type, int(total)))
        acc = correct/float(total)

        # update scores
        scores['acc_%s_%s' % (lang, data_type)] = float(acc)

    def get_blank_acc(self, x_gold, x_pred, x_blank):
        # x_gold  (seq1,batch)
        # x_pred  (seq2,batch)
        # x_blank (seq1,batch)
        if x_gold.is_cuda:
            x_gold = x_gold.cpu()
        if x_pred.is_cuda:
            x_pred = x_pred.cpu()
        if x_blank.is_cuda:
            x_blank = x_blank.cpu()
        num_gold_length = x_gold.shape[0]
        num_batch = x_gold.shape[1]
        diff_len = num_gold_length-x_pred.shape[0]
        if diff_len>0:
          p1d = (0,0,0, diff_len)
          x_pred = torch.nn.functional.pad(x_pred, p1d, "constant", 0)
        if diff_len<0:
          x_pred = x_pred[0:diff_len,:]
        sent_gold = x_gold.numpy()
        sent_pred = x_pred.numpy()
        sent_blank = x_blank.numpy()
        assert sent_pred.shape == sent_gold.shape

        mask = sent_blank == self.blank_index
        total = np.sum(mask)
        correct = sent_gold[mask] == sent_pred[mask]
        correct = np.sum(correct)

        return total, correct



    def eval_para(self, lang1, lang2, data_type, scores, device, params = None, bleu_eval=True):
        """
        Evaluate lang1 -> lang2 perplexity and BLEU scores.
        """
        logger.info("Evaluating Para %s -> %s (%s) ..." % (lang1, lang2, data_type))
        assert data_type in ['valid', 'test']
        self.encoder.eval()
        self.decoder.eval()
        params = self.params
        lang1_id = params.lang2id[lang1]
        lang2_id = params.lang2id[lang2]

        # hypothesis
        txt = []
        txt_pad = []
        txt_tone_enh = []

        # for perplexity
        loss_weight = self.decoder.loss_fn[lang2_id].weight.clone()
        loss_weight[params.sos_index] = 0
        loss_weight[params.sep_index] = 0
        loss_fn2 = nn.CrossEntropyLoss(weight=loss_weight, size_average=False)

        n_words2 = self.params.n_words[lang2_id]
        rytm_correct = 0
        rytm_ntcount = 0
        rytm_total = 0
        count = 0
        xe_loss = 0

        for batch in self.get_iterator(data_type, lang1, lang2):

            # batch
            (sent1, len1), (sent_abs, len_abs), (sent2, len2) = batch
            if data_type.startswith('test'):
                if params.use_summary and lang1 == 'sw':
                    sent1 = sent_abs
                    len1 = len_abs
            sent1, sent2 = sent1.to(device), sent2.to(device)

            # encode / decode / generate
            encoded = self.encoder(sent1, len1, lang1_id)
            decoded = self.decoder(encoded, sent2[:-1], lang2_id)
            # decoded = self.decoder(encoded, sent2[:-1], lang_id=lang2_id, use_lens=params.use_lens, lens=len2.to(device))
            max_len = self.params.max_len[lang2_id]
            sent2_, len2_, _ = self.decoder.generate(encoded, lang2_id, max_len=max_len)
            # sent2_enh, len2_enh = self.double_para(sent2_, len2_, lang2_id, do_pad=self.params.do_pad, do_bos=self.params.do_bos, do_sep=self.params.do_sep)

            a, b = self.eval_rythm(sent2_, do_pad=self.params.do_pad, do_bos=self.params.do_bos, do_sep=self.params.do_sep)
            rytm_correct += a 
            rytm_ntcount += b
            rytm_total += sent1.shape[1]

            # cross-entropy loss
            xe_loss += loss_fn2(decoded.view(-1, n_words2), sent2[1:].view(-1)).item()
            count_dec = 0
            if params.do_bos:
                count_dec = (sent2_==params.sos_index).sum().item()
            if params.do_sep:
                count_dec = (sent2_==params.sep_index).sum().item()
            count += (len2 - 1).sum().item()-count_dec  # skip BOS word
            

            # convert to text
            txt.extend(convert_to_text(sent2_, len2_, self.dico[lang2], lang2_id, self.params, do_pad=self.params.do_pad, do_bos=self.params.do_bos, do_sep=self.params.do_sep))
            txt_pad.extend(convert_to_text(sent2_, len2_, self.dico[lang2], lang2_id, self.params, do_pad=False, do_bos=False, do_sep=False))
            # txt_tone_enh.extend(convert_to_text(sent2_enh, len2_enh, self.dico[lang2], lang2_id, self.params, do_pad=self.params.do_pad, do_bos=self.params.do_bos, do_sep=self.params.do_sep))
        # hypothesis / reference paths
        hyp_name = 'hyp{0}.{1}-{2}.{3}.txt'.format(scores['epoch'], lang1, lang2, data_type)
        hyp_path = os.path.join(params.dump_path, hyp_name)
        hyp_name_pad = 'hyp{0}.{1}-{2}.{3}.pad.txt'.format(scores['epoch'], lang1, lang2, data_type)
        hyp_path_pad = os.path.join(params.dump_path, hyp_name_pad)

        # export sentences to hypothesis file / restore BPE segmentation
        with open(hyp_path, 'w', encoding='utf-8') as f:
            f.write('\n'.join(txt) + '\n')
        with open(hyp_path_pad, 'w', encoding='utf-8') as f:
            f.write('\n'.join(txt_pad) + '\n')

        hyp_name_enh = 'hyp{0}.{1}-{2}.{3}.tone_ehance.txt'.format(scores['epoch'], lang1, lang2, data_type)
        hyp_path_enh = os.path.join(params.dump_path, hyp_name_enh)
        # export sentences to hypothesis file / restore BPE segmentation
        with open(hyp_path_enh, 'w', encoding='utf-8') as f:
            f.write('\n'.join(txt_tone_enh) + '\n')
        # restore_segmentation(hyp_path)

        if bleu_eval:
            ref_path = self.params.para_dataset[('pm', 'sw')][2].replace('pth','txt').replace('XX',lang2)
            print (ref_path)

            # evaluate BLEU score
            bleus = eval_moses_bleu(ref_path, hyp_path)
            logger.info("BLEU %s %s : %f" % (hyp_path, ref_path, bleus[0]))
            logger.info("BLEU-1 : %f" % (bleus[1]))
            logger.info("BLEU-2 : %f" % (bleus[2]))
            logger.info("BLEU-3 : %f" % (bleus[3]))
            logger.info("BLEU-4 : %f" % (bleus[4]))

            # update scores
            scores['bleu_%s_%s_%s' % (lang1, lang2, data_type)] = float(bleus[0])

        # update scores
        scores['ppl_%s_%s_%s' % (lang1, lang2, data_type)] = np.exp(xe_loss / count)
        if (rytm_total-rytm_ntcount) !=0:
            scores['rytm_%s_%s_%s_para' % (lang1, lang2, data_type)] = float(rytm_correct)/(rytm_total-rytm_ntcount)


    def eval_back(self, lang1, lang2, lang3, data_type, scores, device):
        """
        Compute lang1 -> lang2 -> lang3 perplexity and BLEU scores.
        """
        logger.info("Evaluating %s -> %s -> %s (%s) ..." % (lang1, lang2, lang3, data_type))
        assert data_type in ['valid', 'test1', 'test2']
        self.encoder.eval()
        self.decoder.eval()
        params = self.params
        lang1_id = params.lang2id[lang1]
        lang2_id = params.lang2id[lang2]
        lang3_id = params.lang2id[lang3]

        # hypothesis
        txt2 = []
        txt3 = []

        # for perplexity
        loss_weight = self.decoder.loss_fn[lang3_id].weight.clone()
        loss_weight[params.sos_index] = 0
        loss_weight[params.sep_index] = 0
        loss_fn3 = nn.CrossEntropyLoss(weight=loss_weight, size_average=False)
        
        n_words3 = self.params.n_words[lang3_id]
        count = 0
        rytm_correct = 0
        rytm_ntcount = 0
        rytm_total = 0
        xe_loss = 0

        for batch in self.get_iterator(data_type, lang1, lang3):

            # batch
            ((sent1, len1), (sent_abs1, len_abs1)), ((sent3, len3), (sent_abs3, len_abs3)) = batch
            sent1, sent3 = sent1.to(device), sent3.to(device)

            # encode / generate lang1 -> lang2
            encoded = self.encoder(sent1, len1, lang_id=lang1_id)
            max_len = self.params.max_len[lang2_id]
            sent2_, len2_, _ = self.decoder.generate(encoded, lang_id=lang2_id, max_len=max_len)


            # encode / decode / generate lang2 -> lang3
            encoded = self.encoder(sent2_.to(device), len2_, lang2_id)
            decoded = self.decoder(encoded, sent3[:-1], lang3_id)
            # decoded = self.decoder(encoded, sent3[:-1], lang_id=lang3_id, use_lens=params.use_lens, lens=len3.to(device))
            max_len = self.params.max_len[lang3_id]
            sent3_, len3_, _ = self.decoder.generate(encoded, lang3_id, max_len=max_len)

            a, b = self.eval_rythm(sent3_, do_pad=self.params.do_pad, do_bos=self.params.do_bos, do_sep=self.params.do_sep)
            rytm_correct += a 
            rytm_ntcount += b
            rytm_total += sent1.shape[1]

            # cross-entropy loss
            xe_loss += loss_fn3(decoded.view(-1, n_words3), sent3[1:].view(-1)).item()
            count_dec = 0
            if params.do_bos:
                count_dec = (sent3_==params.sos_index).sum().item()
            if params.do_sep:
                count_dec = (sent3_==params.sep_index).sum().item()
            count += (len3 - 1).sum().item()-count_dec  # skip BOS word

            # convert to text
            txt2.extend(convert_to_text(sent2_, len2_, self.dico[lang2], lang2_id, self.params, do_pad=self.params.do_pad, do_bos=self.params.do_bos, do_sep=self.params.do_sep))
            txt3.extend(convert_to_text(sent3_, len3_, self.dico[lang3], lang3_id, self.params, do_pad=self.params.do_pad, do_bos=self.params.do_bos, do_sep=self.params.do_sep))


        # hypothesis / reference paths
        hyp_name2 = 'hyp{0}.{1}-{2}.{3}.txt'.format(scores['epoch'], lang1, lang2, data_type)
        hyp_path2 = os.path.join(params.dump_path, hyp_name2)
        hyp_name3 = 'hyp{0}.{1}-{2}-{3}.{4}.txt'.format(scores['epoch'], lang1, lang2, lang3, data_type)
        hyp_path3 = os.path.join(params.dump_path, hyp_name3)
        # if lang1 == lang3:
        #     _lang1, _lang3 = self.get_pair_for_mono(lang1)
        #     if lang3 != _lang3:
        #         _lang1, _lang3 = _lang3, _lang1
        #     ref_path = params.ref_paths[(_lang1, _lang3, data_type)]
        # else:
        #     ref_path = params.ref_paths[(lang1, lang3, data_type)]
        ref_path = self.params.mono_dataset[lang3][1].replace('pth','txt')
        print (ref_path)

        # export sentences to hypothesis file / restore BPE segmentation
        with open(hyp_path2, 'w', encoding='utf-8') as f:
            f.write('\n'.join(txt2) + '\n')
        with open(hyp_path3, 'w', encoding='utf-8') as f:
            f.write('\n'.join(txt3) + '\n')
        # restore_segmentation(hyp_path)

        # evaluate BLEU score
        bleus = eval_moses_bleu(ref_path, hyp_path3)
        logger.info("BLEU %s %s : %f" % (hyp_path3, ref_path, bleus[0]))
        logger.info("BLEU-1 : %f" % (bleus[1]))
        logger.info("BLEU-2 : %f" % (bleus[2]))
        logger.info("BLEU-3 : %f" % (bleus[3]))
        logger.info("BLEU-4 : %f" % (bleus[4]))

        # update scores
        logger.info("Rythem info: ")
        logger.info(rytm_ntcount)
        logger.info(rytm_total)
        logger.info(float(rytm_correct)/(rytm_total))
        scores['ppl_%s_%s_%s_%s' % (lang1, lang2, lang3, data_type)] = np.exp(xe_loss / count)
        scores['bleu_%s_%s_%s_%s' % (lang1, lang2, lang3, data_type)] = float(bleus[0])
        if rytm_total-rytm_ntcount!=0:
            scores['rytm_%s_%s_%s_%s_back' % (lang1, lang2, lang3, data_type)] = float(rytm_correct)/(rytm_total-rytm_ntcount)

    def run_all_evals(self, epoch, params):
        """
        Run all evaluations.
        """
        scores = OrderedDict({'epoch': epoch})

        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

        lang1 = 'sw'
        lang2 = 'pm'
        data_type = 'test'
        # dump_path = '/home/pengshancai/poem-prose_transfer/dumped/test/4902580/'
        dump_path = '/home/pengshancai/poem-prose_transfer/dumped/test/4909294/'
        dopmpad = True
        tokenizer = self.dico[lang2]
        epochs = range(0,40)

        with torch.no_grad():
            #TODOTODO
            
            for epoch in epochs:
                hyp_name = 'hyp{0}.{1}-{2}.{3}.txt'.format(epoch, lang1, lang2, data_type)
                hyp_path = os.path.join(dump_path, hyp_name)
                logger.info(hyp_path)

                valid_input = []
                with io.open(hyp_path, "r", encoding='utf8') as f:
                    for line in f:
                        s = line.rstrip()
                        valid_input.append(s)

                data, sent, sent_abs = get_data(valid_input, tokenizer, False, dopmpad)

                data['positions'] = data['positions'].numpy()
                n_sentences = len(data['positions'])
                indices = np.arange(n_sentences)
                batches = np.array_split(indices, math.ceil(len(indices) * 1. / 32))

                txt_tone_enh = []
                for sentence_ids in batches:
                    pos = data['positions'][sentence_ids]
                    sents = [data['sentences'][a:b] for a, b in pos]
                    sent2_, len2_ = batch_sentences_pm(sents)
                    sent2_ = sent2_.to(device)
                    lang2_id = 0
                    sent2_enh, len2_enh = self.double_para(sent2_, len2_, lang2_id, do_pad=False, do_bos=dopmpad, do_sep=False)
                    txt_tone_enh.extend(convert_to_text(sent2_enh, len2_enh, self.dico[lang2], lang2_id, self.params, do_pad=False, do_bos=dopmpad, do_sep=False))

                logger.info(len(txt_tone_enh))
                hyp_name_enh = 'hyp{0}.{1}-{2}.{3}.tone_ehance.txt'.format(epoch, lang1, lang2, data_type)
                hyp_path_enh = os.path.join(self.params.dump_path, hyp_name_enh)
                logger.info(hyp_path_enh)
                # export sentences to hypothesis file / restore BPE segmentation
                with open(hyp_path_enh, 'w', encoding='utf-8') as f:
                    f.write('\n'.join(txt_tone_enh) + '\n')

        logger.info('Done!!!')
        return scores

    def eval_transfer(self, lang1, lang2, data_type, scores, device, params = None, bleu_eval=False):
        """
        Evaluate lang1 -> lang2 perplexity and BLEU scores.
        """
        logger.info("Evaluating high quality transfer %s -> %s (%s) ..." % (lang1, lang2, data_type))
        assert data_type in ['valid', 'test1', 'test2']
        self.encoder.eval()
        self.decoder.eval()
        params = self.params
        lang1_id = params.lang2id[lang1]
        lang2_id = params.lang2id[lang2]

        # hypothesis
        txt = []
        txt_tone_enh = []

        # for perplexityloss_weight
        loss_weight = self.decoder.loss_fn[lang2_id].weight.clone()
        loss_weight[params.sos_index] = 0
        loss_weight[params.sep_index] = 0
        loss_fn2 = nn.CrossEntropyLoss(weight=loss_weight, size_average=False)
        
        n_words2 = self.params.n_words[lang2_id]
        rytm_correct = 0
        rytm_ntcount = 0
        rytm_total = 0

        for batch in self.get_iterator(data_type, lang1, None):

            # batch
            (sent1, len1), (sent_abs, len_abs) = batch
            if data_type.startswith('test'):
                if params.use_summary and lang1 == 'sw':
                    sent1 = sent_abs
                    len1 = len_abs
            sent1 = sent1.to(device)

            # encode / decode / generate
            encoded = self.encoder(sent1, len1, lang1_id)
            max_len = self.params.max_len[lang2_id]
            sent2_, len2_, _ = self.decoder.generate(encoded, lang2_id, max_len=max_len)
            # sent2_enh, len2_enh = self.double_para(sent2_, len2_, lang2_id, do_pad=self.params.do_pad, do_bos=self.params.do_bos, do_sep=self.params.do_sep)

            a, b = self.eval_rythm(sent2_, do_pad=self.params.do_pad, do_bos=self.params.do_bos, do_sep=self.params.do_sep)
            rytm_correct += a 
            rytm_ntcount += b
            rytm_total += sent1.shape[1]

            # convert to text
            txt.extend(convert_to_text(sent2_, len2_, self.dico[lang2], lang2_id, self.params, do_pad=self.params.do_pad, do_bos=self.params.do_bos, do_sep=self.params.do_sep))
            # txt_tone_enh.extend(convert_to_text(sent2_enh, len2_enh, self.dico[lang2], lang2_id, self.params, do_pad=self.params.do_pad, do_bos=self.params.do_bos, do_sep=self.params.do_sep))

        # hypothesis / reference paths
        hyp_name = 'hyp{0}.{1}-{2}.{3}.txt'.format(scores['epoch'], lang1, lang2, data_type)
        hyp_path = os.path.join(params.dump_path, hyp_name)
        # export sentences to hypothesis file / restore BPE segmentation
        with open(hyp_path, 'w', encoding='utf-8') as f:
            f.write('\n'.join(txt) + '\n')
        hyp_name_enh = 'hyp{0}.{1}-{2}.{3}.tone_ehance.txt'.format(scores['epoch'], lang1, lang2, data_type)
        hyp_path_enh = os.path.join(params.dump_path, hyp_name_enh)
        # export sentences to hypothesis file / restore BPE segmentation
        with open(hyp_path_enh, 'w', encoding='utf-8') as f:
            f.write('\n'.join(txt_tone_enh) + '\n')

        # if bleu_eval:
        #     ref_path = self.params.mono_dataset[lang2][1].replace('pth','txt') #potential bug here
        #     print (ref_path)

        #     # evaluate BLEU score
        #     bleus = eval_moses_bleu(ref_path, hyp_path)
        #     logger.info("BLEU %s %s : %f" % (hyp_path, ref_path, bleus[0]))
        #     logger.info("BLEU-1 : %f" % (bleus[1]))
        #     logger.info("BLEU-2 : %f" % (bleus[2]))
        #     logger.info("BLEU-3 : %f" % (bleus[3]))
        #     logger.info("BLEU-4 : %f" % (bleus[4]))

        #     # update scores
        #     scores['bleu_%s_%s_%s' % (lang1, lang2, data_type)] = float(bleus[0])

        logger.info("Rythem info: ")
        logger.info(rytm_ntcount)
        logger.info(rytm_total)
        logger.info(float(rytm_correct)/(rytm_total))
        if rytm_total-rytm_ntcount!=0:
            scores['rytm_%s_%s_%s_trans' % (lang1, lang2, data_type)] = float(rytm_correct)/(rytm_total-rytm_ntcount)


def _parse_multi_bleu_ret(bleu_str, return_all=False):
    bleu_score = re.search(r"BLEU = (.+?),", bleu_str).group(1)
    bleu_score = np.float32(bleu_score)

    if return_all:
        bleus = re.search(r", (.+?)/(.+?)/(.+?)/(.+?) ", bleu_str)
        bleus = [bleus.group(group_idx) for group_idx in range(1, 5)]
        bleus = [np.float32(b) for b in bleus]
        bleu_score = [bleu_score] + bleus

    return bleu_score


def eval_moses_bleu(ref, hyp):
    """
    Given a file of hypothesis and reference files,
    evaluate the BLEU score using Moses scripts.
    """
    assert os.path.isfile(ref) and os.path.isfile(hyp)
    command = BLEU_SCRIPT_PATH + ' %s < %s'
    p = subprocess.Popen(command % (ref, hyp), stdout=subprocess.PIPE, shell=True)
    result = p.communicate()[0].decode("utf-8")
    if result.startswith('BLEU'):
        return _parse_multi_bleu_ret(result, return_all=True)
    else:
        logger.warning('Impossible to parse BLEU score! "%s"' % result)
        return -1


def convert_to_text(batch, lengths, dico, lang_id, params, do_pad=False, do_bos=False, do_sep=False):
    """
    Convert a batch of sentences to a list of text sentences.
    """
    batch = batch.cpu().numpy()
    lengths = lengths.cpu().numpy()
    bos_index = params.bos_index[lang_id]
    lang = params.id2lang[lang_id]

    slen, bs = batch.shape
    assert lengths.max() == slen and lengths.shape[0] == bs
    assert (batch[0] == bos_index).sum() == bs
    assert (batch == params.eos_index).sum() == bs
    sentences = []

    # for j in range(bs):
    #     words = []
    #     tmp_idcs = batch[:, j]
    #     tmp_idcs = np.append(tmp_idcs,[-1,0,0])
    #     # logger.info(tmp_idcs)
    #     # logger.info(len(tmp_idcs))
    #     alist= zip(tmp_idcs[1::4], tmp_idcs[2::4], tmp_idcs[3::4], tmp_idcs[4::4])
    #     for a,b,c,d in alist:
    #         if a == params.eos_index:
    #             break
    #         if a != 0:
    #             token = dico[a]
    #             if token.startswith('##'):
    #                 # logger.warning('Impossible !!! This code is not ready for this yet at training. ask Zhichao for more')
    #                 token=token[2:]
    #             words.append(token)
    #         if b == params.eos_index:
    #             break
    #         if b != 0:
    #             token = dico[b]
    #             if token.startswith('##'):
    #                 # logger.warning('Impossible !!! This code is not ready for this yet at training. ask Zhichao for more')
    #                 token=token[2:]
    #             words.append(token)
    #     sentences.append("".join(words))

    if do_pad and lang=='pm':
        for j in range(bs):
            words = []
            for k in range(1, lengths[j]):
                tmp_idx = batch[k, j]
                if tmp_idx == params.eos_index:
                    break
                if tmp_idx != params.pad_index:
                    token = dico[tmp_idx]
                    if token.startswith('##'):
                        # logger.warning('Impossible !!! This code is not ready for this yet at training. ask Zhichao for more')
                        token=token[2:]
                    words.append(token)
            sentences.append("".join(words))
    elif do_bos and lang=='pm':
        for j in range(bs):
            words = []
            for k in range(1, lengths[j]):
                tmp_idx = batch[k, j]
                if tmp_idx == params.eos_index:
                    break
                if tmp_idx != params.sos_index:
                    token = dico[tmp_idx]
                    if token.startswith('##'):
                        # logger.warning('Impossible !!! This code is not ready for this yet at training. ask Zhichao for more')
                        token=token[2:]
                    words.append(token)
            sentences.append("".join(words))
    elif do_sep and lang=='pm':
        for j in range(bs):
            words = []
            for k in range(1, lengths[j]):
                tmp_idx = batch[k, j]
                if tmp_idx == params.eos_index:
                    break
                if tmp_idx != params.sep_index:
                    token = dico[tmp_idx]
                    if token.startswith('##'):
                        # logger.warning('Impossible !!! This code is not ready for this yet at training. ask Zhichao for more')
                        token=token[2:]
                    words.append(token)
            sentences.append("".join(words))
    else:
        for j in range(bs):
            words = []
            for k in range(1, lengths[j]):
                tmp_idx = batch[k, j]
                if tmp_idx == params.eos_index:
                    break
                token = dico[tmp_idx]
                if token.startswith('##'):
                    # logger.warning('Impossible !!! This code is not ready for this yet at training. ask Zhichao for more')
                    token=token[2:]
                words.append(token)
            sentences.append("".join(words))


    return sentences
