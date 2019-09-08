import logging
from itertools import accumulate
from itertools import chain

import chainer
import chainer.functions as F
import chainer.links as L
from chainer import initializers, reporter

logger = logging.getLogger(__name__)


class CharLSTM_Encoder(chainer.Chain):
    def __init__(
        self,
        n_char_vocab: int,
        n_layers: int,
        char_dim: int,
        hidden_dim: int,
        dropout_rate: float,
        char_initializer=None
    ):
        super(CharLSTM_Encoder, self).__init__()

        with self.init_scope():
            self.char_embed = L.EmbedID(
                n_char_vocab,
                char_dim,
                initialW=char_initializer
            )

            self.char_level_bilstm = L.NStepBiLSTM(
                n_layers,
                char_dim,
                hidden_dim,
                dropout_rate
            )

    def forward(self, char_inputs):
        batch_size = len(char_inputs)
        offsets = list(accumulate(len(w) for w in char_inputs))
        char_embs_flatten = self.char_embed(
            self.xp.concatenate(char_inputs, axis=0))
        char_embs = F.split_axis(char_embs_flatten, offsets[:-1], axis=0)

        hs, _, _ = self.char_level_bilstm(None, None, char_embs)
        char_features = hs.transpose((1, 0, 2))
        char_features = char_features.reshape(batch_size, -1)
        return char_features


class BiLSTM_CRF(chainer.Chain):
    """
    BiLSTM-CRF: Bidirectional LSTM + Conditional Random Field as a decoder
    """

    def __init__(
            self,
            configs,
            num_word_vocab,
            num_char_vocab,
            num_tag_vocab
    ):

        super(BiLSTM_CRF, self).__init__()
        if "model" not in configs:
            raise Exception("Model configurations are not found")

        model_configs = configs["model"]

        model_configs["num_word_vocab"] = num_word_vocab
        model_configs["num_char_vocab"] = num_char_vocab
        model_configs["num_tag_vocab"] = num_tag_vocab

        # word encoder
        self.num_word_vocab = model_configs.get("num_word_vocab")
        self.word_dim = model_configs.get("word_dim")
        self.word_hidden_dim = model_configs.get("word_hidden_dim")

        # char encoder
        self.num_char_vocab = model_configs.get("num_char_vocab")
        self.num_char_hidden_layers = 1
        self.char_dim = model_configs.get("char_dim")
        self.char_hidden_dim = model_configs.get("char_hidden_dim")

        # integrated word encoder
        self.num_word_hidden_layers = 1  # same as Lample
        self.word_hidden_dim = model_configs.get("word_hidden_dim")

        # transformer
        self.linear_input_dim = 0

        # decoder
        self.num_tag_vocab = model_configs.get("num_tag_vocab")

        # feature extractor (BiLSTM)
        self.internal_hidden_dim = 0
        self.dropout_rate = model_configs.get("dropout", 0)

        # param initializer
        # approx: https://github.com/glample/tagger/blob/master/utils.py#L44
        self.initializer = initializers.GlorotUniform()

        # setup links with given params
        with self.init_scope():
            self._setup_word_encoder()
            self._setup_char_encoder()
            self._setup_feature_extractor()
            self._setup_decoder()

        logger.debug(f"Dropout rate: \x1b[31m{self.dropout_rate}\x1b[0m")
        logger.debug(f"Word embedding dim: \x1b[31m{self.word_dim}\x1b[0m")
        logger.debug(f"Char embedding dim: \x1b[31m{self.char_dim}\x1b[0m")

    def set_pretrained_word_vectors(self, syn0):
        self.embed_word.W.data = syn0

    def _setup_word_encoder(self):
        if self.word_dim is None:
            return

        logger.debug("Use word level encoder")
        self.embed_word = L.EmbedID(
            self.num_word_vocab,
            self.word_dim,
            initialW=self.initializer
        )

    def _setup_char_encoder(self):
        if self.char_dim is None:
            return

        logger.debug("Use character level encoder")
        self.char_level_encoder = CharLSTM_Encoder(
            self.num_char_vocab,
            self.num_char_hidden_layers,
            self.char_dim,
            self.char_hidden_dim,
            self.dropout_rate,
            char_initializer=self.initializer,
        )
        self.internal_hidden_dim += 2 * self.char_hidden_dim

    def _setup_feature_extractor(self):
        # ref: https://github.com/glample/tagger/blob/master/model.py#L256
        self.internal_hidden_dim += self.word_hidden_dim
        self.linear_input_dim += 2 * self.word_hidden_dim

        self.word_level_bilstm = L.NStepBiLSTM(
            self.num_word_hidden_layers,
            self.internal_hidden_dim,
            self.word_hidden_dim,
            self.dropout_rate,
        )

        self.linear = L.Linear(
            self.linear_input_dim,
            self.num_tag_vocab,
            initialW=self.initializer
        )

    def _setup_decoder(self):
        self.crf = L.CRF1d(self.num_tag_vocab, initial_cost=self.initializer)

    def forward(self, inputs, outputs, **kwargs):
        features = self.__extract__(inputs, **kwargs)
        loss = self.crf(features, outputs, transpose=True)

        _, pathes = self.crf.argmax(features, transpose=True)
        reporter.report({"loss": loss}, self)
        return loss

    def predict(self, batch, **kwargs):
        features = self.__extract__(batch)
        _, pathes = self.crf.argmax(features, transpose=True)
        return pathes

    def word_encode(self, word_sentence):
        return self.embed_word(word_sentence)

    def char_encode(self, char_inputs, **kwargs):
        return self.char_level_encoder(char_inputs)

    def __extract__(self, batch, **kwargs):
        """
        :param batch: list of list, inputs
        inputs: (word_sentences, char_sentences)
        """
        word_sentences, char_sentences = batch
        offsets = list(accumulate(len(s) for s in word_sentences))

        lstm_inputs = []
        if self.word_dim is not None:
            word_repr = self.word_encode(
                self.xp.concatenate(word_sentences, axis=0))
            word_repr = F.dropout(word_repr, self.dropout_rate)
            lstm_inputs.append(word_repr)
        if self.char_dim is not None:
            # NOTE [[list[int]]] -> [list[int]]
            flatten_char_sentences = list(chain.from_iterable(char_sentences))
            char_repr = self.char_encode(flatten_char_sentences)
            char_repr = F.dropout(char_repr, self.dropout_rate)
            lstm_inputs.append(char_repr)
        lstm_inputs = F.split_axis(
            F.concat(lstm_inputs, axis=1), offsets[:-1], axis=0)

        _, _, hs = self.word_level_bilstm(None, None, lstm_inputs)
        features = [self.linear(h) for h in hs]
        return features
