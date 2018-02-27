from keras import optimizers
from keras.models import Model
from keras.models import Input
from keras.layers import LSTM
from keras.layers import Embedding
from keras.layers import Dense
from keras.layers import TimeDistributed
from keras.layers import Dropout
from keras.layers import Bidirectional
from keras_contrib.layers.crf import CRF

from sklearn.model_selection import train_test_split
from sklearn.model_selection import KFold

from utils_models import compile_model
from metrics import Metrics

# https://stackoverflow.com/questions/48615003/multi-task-learning-in-keras
# https://machinelearningmastery.com/keras-functional-api-deep-learning/

# TODO (johngiorgi): not clear if I need to clear non-shared layers when
# building the model
# TODO (johngiorgi): read up on train_test_split, do I want to shuffle?
# Do I want to stratify? look at the intro ch in my deep learning book
# TODO (johngiorgi): the way I get train/test partitions is likely copying
# huge lists

class MultiTaskLSTMCRF(object):
    """ Implements a MT biLSTM-CRF for NER and TWI using Keras. """

    def __init__(self, model_specifications):
        # Grab the specs we need to build the model. Keys correspond to names of
        # attributes of Dataset and SequenceProcessingModel classes.
        self.activation_function = model_specifications['activation_function']
        self.batch_size = model_specifications['batch_size']
        self.dropout_rate = model_specifications['dropout_rate']
        self.freeze_token_embeddings = model_specifications['freeze_token_embeddings']
        self.gradient_clipping_value = model_specifications['gradient_clipping_value']
        self.k_folds = model_specifications['k_folds']
        self.learning_rate = model_specifications['learning_rate']
        self.maximum_number_of_epochs = model_specifications['maximum_number_of_epochs']
        self.optimizer = model_specifications['optimizer']
        self.token_embedding_matrix = model_specifications['token_embedding_matrix']
        self.token_embedding_dimension = model_specifications['token_embedding_dimension']
        self.max_seq_len = model_specifications['max_seq_len']

        # dataset(s) tied to this instance
        self.ds = model_specifications['ds']
        # model(s) tied to this instance
        self.model = []
        self.crf = []

    def specify_(self):
        """ Specifies a multi-task bidirectional LSTM-CRF for sequence tagging
        using Keras.

        Implements a hybrid long short-term memory network-condition random
        field (LSTM-CRF) multi-task model for sequence tagging.

        Returns:
            model: a list of keras models, sharing (excluding crf layer) sharing
                   some number of layers.
            crf: a list of task-specific crf layers implemented using
                 keras.contrib, one for each model.
        """
        ## TOKEN EMBEDDING LAYER
        # if specified, load pre-trained token embeddings otherwise initialize
        # randomly
        if self.token_embedding_matrix is not None:
            # plus 1 because of '0' word.
            shared_token_emb = Embedding(
                # input dimension size of all word types shared across datasets
                input_dim=len(self.ds[0].word_type_to_idx) + 1,
                output_dim=self.token_embedding_matrix.shape[1],
                weights=[self.token_embedding_matrix],
                input_length=self.max_seq_len,
                mask_zero=True,
                trainable=(not self.freeze_token_embeddings))
        else:
            shared_token_emb = Embedding(
                input_dim=len(self.ds[0].word_type_to_idx) + 1,
                output_dim=self.token_embedding_dimension,
                input_length=self.max_seq_len,
                mask_zero=True)
        ## TOKEN BILSTM LAYER
        shared_token_bisltm = Bidirectional(LSTM(
            units=100,
            return_sequences=True,
            recurrent_dropout=self.dropout_rate))
        ## FULLY CONNECTED LAYER
        shared_dense = TimeDistributed(Dense(
            units=100,
            activation=self.activation_function))

        # specify model, taking into account the shared layers
        for ds in self.ds:
            input_layer = Input(shape=(self.max_seq_len,))
            model = shared_token_emb(input_layer)
            model = shared_token_bisltm(model)
            model = shared_dense(model)
            crf = CRF(ds.tag_type_count)
            output_layer = crf(model)

            # fully specified model
            self.model.append(Model(inputs=input_layer, outputs=output_layer))
            self.crf.append(crf)

            # clear all non-shared layers
            input_layer = None
            model = None
            crf = None
            output_layer = None

        return self.model

    def compile_(self):
        """ Compiles a bidirectional multi-task LSTM-CRF for for sequence
        tagging using Keras. """
        for model, crf in zip(self.model, self.crf):
            compile_model(model=model,
                          learning_rate=self.learning_rate,
                          optimizer=self.optimizer,
                          loss_function=crf.loss_function,
                          metrics=crf.accuracy)

    def fit_(self, checkpointer):
        """ Fits a bidirectional multi-task LSTM-CRF for for sequence tagging
        using Keras. """
        # get indices of a k fold split training set
        # for each fold in this split
        # for each epoch in global epochs
        # for each dataset and model
        # fit the model, compute metrics for train/valid sets
        # get average performance scores for this fold
        # empty the model
        # specify and compile again
        # repeate
        train_valid_indices = self._get_train_valid_indices()

        for fold in range(self.k_folds):
            for epoch in range(self.maximum_number_of_epochs):
                for i, (ds, model) in enumerate(zip(self.ds, self.model)):
                    X = ds.train_word_idx_sequence
                    y = ds.train_tag_idx_sequence
                    # train_valid_indices[i][fold] is a two-tuple, where index
                    # 0 contains the train indicies and index 1 the valid
                    # indicies
                    X_train = X[train_valid_indices[i][fold][0]]
                    X_valid = X[train_valid_indices[i][fold][1]]
                    y_train = y[train_valid_indices[i][fold][0]]
                    y_valid = y[train_valid_indices[i][fold][1]]

                    model.fit(x=X_train,
                              y=y_train,
                              batch_size=self.batch_size,
                              epochs=1,
                              callbacks=[
                                checkpointer,
                                Metrics(X_valid, y_valid, ds.tag_type_to_idx)],
                              validation_data=[X_valid, y_valid],
                              verbose=1)

    def _get_train_valid_indices(self):
        """
        Get train and valid indicies for all k folds for all datasets.

        For all datatsets self.ds, gets stratified k-folds train and valid
        indicies (number of k_folds specified by self.k_folds). Returns a list
        of list of tuples, where the outer list is of length len(self.ds),
        the inner list is of length len(self.k_folds) and contains two-tuples
        corresponding to train indicies and valid indicies respectively.
        """
        # acc
        compound_train_valid_indices = []
        # Sklearn KFold object
        kf = KFold(n_splits=self.k_folds, random_state=42)

        for ds in self.ds:
            dataset_train_valid_indices = []
            for train_idx, valid_idx in kf.split(ds.train_word_idx_sequence):
                dataset_train_valid_indices.append((train_idx, valid_idx))
            compound_train_valid_indices.append(dataset_train_valid_indices)

        return compound_train_valid_indices
