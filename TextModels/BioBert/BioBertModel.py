import codecs
import pickle

import keras
import numpy as np
import pandas as pd
import tensorflow as tf
from keras import Input, Model
from keras import backend as K
from keras.callbacks import ModelCheckpoint
from keras.layers import Layer, Bidirectional, Embedding, Dense, Flatten
from keras_bert import load_trained_model_from_checkpoint
from keras_multi_head import MultiHeadAttention
from keras_preprocessing.sequence import pad_sequences
from metrics import roc_callback, \
    auc_roc
from multiplicative_lstm import MultiplicativeLSTM
from keras_bert import load_trained_model_from_checkpoint, Tokenizer

from CiclicLR import CyclicLR

from TextModels.BioBert.datagenerator import DataGenerator

MAX_WORDS_TEXT = 500
WORD_EMBEDDINGS_SIZE = 200
EPOCHS = 30
BATCH_SIZE = 64
TRAIN = "../../Datasets/MIMIC-III/train.csv"
TEST = "../../Datasets/MIMIC-III/test.csv"
VAL = "../../Datasets/MIMIC-III/val.csv"


def auc_roc(y_true, y_pred):
    # any tensorflow metric
    value, update_op = tf.metrics.auc(y_true, y_pred)

    # find all variables created for this metric
    metric_vars = [i for i in tf.local_variables() if 'auc_roc' in i.name.split('/')[1]]

    # Add metric variables to GLOBAL_VARIABLES collection.
    # They will be initialized for new session.
    for v in metric_vars:
        tf.add_to_collection(tf.GraphKeys.GLOBAL_VARIABLES, v)

    # force to update metric values
    with tf.control_dependencies([update_op]):
        value = tf.identity(value)
        return value


class Pentanh(Layer):
    def __init__(self, **kwargs):
        super(Pentanh, self).__init__(**kwargs)
        self.supports_masking = True
        self.__name__ = 'pentanh'

    def call(self, inputs):
        return K.switch(K.greater(inputs, 0), K.tanh(inputs), 0.25 * K.tanh(inputs))

    def get_config(self):
        return super(Pentanh, self).get_config()

    def compute_output_shape(self, input_shape):
        return input_shape


def loadData():
    traindf = pd.read_csv(TRAIN)
    x_train = traindf["TEXT"].values
    y_train = traindf[['No Finding', 'Enlarged Cardiomediastinum', 'Cardiomegaly', 'Airspace Opacity',
                       'Lung Lesion', 'Edema', 'Consolidation', 'Pneumonia', 'Atelectasis', 'Pneumothorax',
                       'Pleural Effusion',
                       'Pleural Other', 'Fracture', 'Support Devices']].values

    valdf = pd.read_csv(VAL)
    x_val = valdf["TEXT"].values
    y_val = valdf[['No Finding', 'Enlarged Cardiomediastinum', 'Cardiomegaly', 'Airspace Opacity',
                   'Lung Lesion', 'Edema', 'Consolidation', 'Pneumonia', 'Atelectasis', 'Pneumothorax',
                   'Pleural Effusion',
                   'Pleural Other', 'Fracture', 'Support Devices']].values

    testdf = pd.read_csv(TEST)
    x_test = testdf["TEXT"].values
    y_test = testdf[['No Finding', 'Enlarged Cardiomediastinum', 'Cardiomegaly', 'Airspace Opacity',
                     'Lung Lesion', 'Edema', 'Consolidation', 'Pneumonia', 'Atelectasis', 'Pneumothorax',
                     'Pleural Effusion',
                     'Pleural Other', 'Fracture', 'Support Devices']].values

    print(len(x_train), 'train sequences')
    print(len(x_val), 'val sequences')
    print(len(x_test), 'test sequences')

    return x_train, y_train, x_val, y_val, x_test, y_test,testdf


def getModel(voc_size, embedding_matrix):
    input_layer = Input(name='Input', shape=(MAX_WORDS_TEXT,), dtype="float32")

    embedding_layer = Embedding(voc_size, WORD_EMBEDDINGS_SIZE, weights=[embedding_matrix],
                                input_length=MAX_WORDS_TEXT, trainable=True)(input_layer)

    rnn_layer = Bidirectional(MultiplicativeLSTM(WORD_EMBEDDINGS_SIZE,
                                                 return_sequences=True, dropout=0.2,
                                                 recurrent_dropout=0.2,
                                                 activation='pentanh',
                                                 recurrent_activation='pentanh'),
                              merge_mode='concat')(embedding_layer)

    attention_layer = MultiHeadAttention(head_num=4)(rnn_layer)
    flatten = Flatten()(attention_layer)
    final = Dense(14, activation='sigmoid')(flatten)

    mdl = Model(inputs=input_layer, outputs=final)
    mdl.compile(loss='binary_crossentropy',
                optimizer='adam',
                metrics=['accuracy', auc_roc])

    print(mdl.summary())
    return mdl


def prepare_embeddings(t, vocab_size, model):
    embedding_matrix = np.zeros((vocab_size, WORD_EMBEDDINGS_SIZE))
    for word, i in t.word_index.items():
        embedding_matrix[i] = model.wv[word]
    reverse_word_map = dict(map(reversed, t.word_index.items()))

    print("Saving tokenizer")
    with open('tokenizer.pickle', 'wb') as handle:
        pickle.dump(t, handle, protocol=pickle.HIGHEST_PROTOCOL)

    print("Saving embeddings of corpus")
    with open('embedding_matrix.pickle', 'wb') as f:
        pickle.dump(embedding_matrix, f, protocol=pickle.HIGHEST_PROTOCOL)

    return embedding_matrix, reverse_word_map


def getTokenEmbed():
    print("Load tokenizer")
    with open('tokenizer.pickle', 'rb') as handle:
        tokenizer = pickle.load(handle)

    print("Load embedding_matrix")
    with open('embedding_matrix.pickle', 'rb') as f:
        embedding_matrix = pickle.load(f)

    voc_size = len(tokenizer.word_index) + 1
    return tokenizer, embedding_matrix, voc_size


def preprocessTexts(x_train, x_val, x_test, tokenizer):
    x_train = tokenizer.texts_to_sequences(x_train)
    x_train = pad_sequences(x_train, maxlen=MAX_WORDS_TEXT, padding='post')

    x_val = tokenizer.texts_to_sequences(x_val)
    x_val = pad_sequences(x_val, maxlen=MAX_WORDS_TEXT, padding='post')

    x_test = tokenizer.texts_to_sequences(x_test)
    x_test = pad_sequences(x_test, maxlen=MAX_WORDS_TEXT, padding='post')

    return x_train, x_val, x_test


def loadBertModel():
    keras.utils.generic_utils.get_custom_objects().update({'pentanh': Pentanh()})
    input = Input((500, 3072))
    rnn_layer = Bidirectional(MultiplicativeLSTM(256,
                                                 return_sequences=True, dropout=0.2,
                                                 recurrent_dropout=0.2,
                                                 activation='pentanh',
                                                 recurrent_activation='pentanh'),
                              merge_mode='concat')(input)

    attention_layer = MultiHeadAttention(head_num=4)(rnn_layer)
    removeMask = Flatten()(attention_layer)
    #dense = Dense(256, activation='relu', kernel_initializer='he_uniform')(removeMask)
    final = Dense(14, activation='sigmoid')(removeMask)

    model_complete = Model(inputs=input, outputs=final)
    model_complete.compile(optimizer='adam',loss='binary_crossentropy',
                  metrics=['accuracy', auc_roc])

    print(model_complete.summary())
    #pool_layer = MaskedGlobalMaxPool1D(name='Pooling')(model.output)
    #model = keras.models.Model(inputs=model.inputs, outputs=pool_layer)
    return model_complete

def prepareTokenizer():
    dict_path = "./biobert_v1.1_pubmed/vocab.txt"
    token_dict = {}
    with codecs.open(dict_path, 'r', 'utf8') as reader:
        for line in reader:
            token = line.strip()
            token_dict[token] = len(token_dict)

    #print(token_dict)
    tokenizer = Tokenizer(token_dict)
    return tokenizer


if __name__ == '__main__':
    keras.utils.generic_utils.get_custom_objects().update({'pentanh': Pentanh()})

    print('Loading data...')
    x_train, y_train, x_val, y_val, x_test, y_test,testdf = loadData()
    print('Loading Tokenizer, Embedding...')
    #tokenizer, embedding_matrix, voc_size = getTokenEmbed()
    tokenizer = prepareTokenizer()
    print('Preprocessing Texts...')
    #x_train, x_val, x_test = preprocessTexts(x_train, x_val, x_test, tokenizer)
    config_path = "./biobert_v1.1_pubmed/bert_config.json"
    checkpoint_path = "./biobert_v1.1_pubmed/model.ckpt-1000000"
    bert = load_trained_model_from_checkpoint(config_path, checkpoint_path, training=False,
                                              output_layer_num=4, seq_len=500)
    dataGen = DataGenerator(x_train, y_train, tokenizer, bert, tf.get_default_graph(), BATCH_SIZE,
                            True)
    dataGenval = DataGenerator(x_val, y_val, tokenizer, bert, tf.get_default_graph(), BATCH_SIZE,
                                True)
    dataGentest = DataGenerator(x_test, y_test, tokenizer, bert, tf.get_default_graph(), BATCH_SIZE,
                                True)

    print("Preparing model")
    model = loadBertModel()

    print("Preparing Training:")
    filepath = "BioBertTextModel-{epoch:02d}-{val_loss:.2f}.hdf5"
    clr = CyclicLR(base_lr=0.00005, max_lr=0.0005, step_size=5000)
    modelckp = ModelCheckpoint(filepath, monitor='val_loss', verbose=1, save_best_only=False,
                               mode='min')

    # es = EarlyStopping(monitor='val_acc', mode='max', min_delta=1,patience=3)
    call_list = [modelckp, clr, roc_callback(dataGentest, testdf)]

    model.fit_generator(dataGen,
                        validation_data=dataGenval,
                        use_multiprocessing=False,
                        verbose=1,
                        epochs=EPOCHS,
                        callbacks=call_list,
                        workers=1)

    model.save_weights("BioBertTextModel.h5")