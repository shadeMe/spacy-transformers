from typing import Union, List, Tuple, TypeVar, Callable, Any, Optional, Sequence
from spacy.pipeline import Pipe
from thinc.neural.ops import get_array_module
from thinc.neural._classes.model import Model
from thinc.api import chain, layerize, wrap
from spacy.util import minibatch
from spacy.tokens import Doc, Span
import numpy

from .wrapper import PyTT_Wrapper
from .util import batch_by_length, pad_batch, flatten_list, unflatten_list, Activations
from .util import pad_batch_activations
from .util import Array, Optimizer, Dropout


class PyTT_TokenVectorEncoder(Pipe):
    """spaCy pipeline component to use PyTorch-Transformers models.

    The component assigns the output of the transformer to the `doc._.pytt_outputs`
    extension attribute. We also calculate an alignment between the word-piece
    tokens and the spaCy tokenization, so that we can use the last hidden states
    to set the doc.tensor attribute. When multiple word-piece tokens align to
    the same spaCy token, the spaCy token receives the sum of their values.
    """

    name = "pytt_tok2vec"

    @classmethod
    def from_nlp(cls, nlp, **cfg):
        """Factory to add to Language.factories via entry point."""
        return cls(nlp.vocab, **cfg)

    @classmethod
    def from_pretrained(cls, vocab, name, **cfg):
        """Create a PyTT_TokenVectorEncoder instance using pre-trained weights
        from a PyTorch Transformer model, even if it's not installed as a
        spaCy package.

        vocab (spacy.vocab.Vocab): The spaCy vocab to use.
        name (unicode): Name of pre-trained model, e.g. 'bert-base-uncased'.
        RETURNS (PyTT_TokenVectorEncoder): The token vector encoder.
        """
        cfg["from_pretrained"] = True
        cfg["pytt_name"] = name
        model = cls.Model(**cfg)
        self = cls(vocab, model=model, **cfg)
        return self

    @classmethod
    def Model(cls, **cfg) -> PyTT_Wrapper:
        """Create an instance of `PyTT_Wrapper`, which holds the
        PyTorch-Transformers model.

        **cfg: Optional config parameters.
        RETURNS (thinc.neural.Model): The wrapped model.
        """
        name = cfg.get("pytt_name")
        if not name:
            raise ValueError("Need pytt_name argument, e.g. 'bert-base-uncased'")
        if cfg.get("from_pretrained"):
            model = PyTT_Wrapper.from_pretrained(name)
        else:
            model = PyTT_Wrapper(name)
        nO = model.nO
        batch_by_length = cfg.get("batch_by_length", 10)
        model = foreach_sentence(
            chain(
                get_word_pieces,
                with_length_batching(model, batch_by_length)
            ))
        model.nO = nO
        return model

    def __init__(self, vocab, model=True, **cfg):
        """Initialize the component.

        model (thinc.neural.Model / True): The component's model or `True` if
            not initialized yet.
        **cfg: Optional config parameters.
        """
        self.vocab = vocab
        self.model = model
        self.cfg = cfg

    def __call__(self, doc):
        """Process a Doc and assign the extracted features.

        doc (spacy.tokens.Doc): The Doc to process.
        RETURNS (spacy.tokens.Doc): The processed Doc.
        """
        self.require_model()
        outputs = self.predict([doc])
        self.set_annotations([doc], outputs)
        return doc

    def pipe(self, stream, batch_size=128):
        """Process Doc objects as a stream and assign the extracted features.

        stream (iterable): A stream of Doc objects.
        batch_size (int): The number of texts to buffer.
        YIELDS (spacy.tokens.Doc): Processed Docs in order.
        """
        for docs in minibatch(stream, size=batch_size):
            docs = list(docs)
            outputs = self.predict(docs)
            self.set_annotations(docs, outputs)
            for doc in docs:
                yield doc

    def begin_update(self, docs, drop=None, **cfg):
        """Get the predictions and a callback to complete the gradient update.
        This is only used internally within PyTT_Language.update.
        """
        outputs, backprop = self.model.begin_update(docs, drop=drop)

        def finish_update(docs, sgd=None):
            gradients = []
            for doc in docs:
                gradients.append(
                    Activations(
                        doc._.pytt_d_last_hidden_state,
                        None, None, None,
                        is_grad=True))
            backprop(gradients, sgd=sgd)
            for doc in docs:
                doc._.pytt_d_last_hidden_state.fill(0)
            return None

        return outputs, finish_update

    def predict(self, docs):
        """Run the transformer model on a batch of docs and return the
        extracted features.

        docs (iterable): A batch of Docs to process.
        RETURNS (list): A list of Activations objects, one per doc.
        """
        return self.model.predict(docs)

    def set_annotations(self, docs, activations):
        """Assign the extracted features to the Doc objects and overwrite the
        vector and similarity hooks.

        docs (iterable): A batch of `Doc` objects.
        activations (iterable): A batch of activations.
        """
        for doc, doc_acts in zip(docs, activations):
            wp_tensor = doc_acts.last_hidden_state
            doc.tensor = self.model.ops.allocate((len(doc), self.model.nO))
            doc._.pytt_last_hidden_state = wp_tensor
            assert wp_tensor.shape == (
                len(doc._.pytt_word_pieces),
                self.model.nO,
            ), (len(doc._.pytt_word_pieces), self.model.nO, wp_tensor.shape)
            # Count how often each word-piece token is represented. This allows
            # a weighted sum, so that we can make sure doc.tensor.sum()
            # equals wp_tensor.sum().
            align_sizes = [0 for _ in range(len(doc._.pytt_word_pieces))]
            for word_piece_slice in doc._.pytt_alignment:
                for i in word_piece_slice:
                    align_sizes[i] += 1
            for i, word_piece_slice in enumerate(doc._.pytt_alignment):
                for j in word_piece_slice:
                    doc.tensor[i] += wp_tensor[j] / align_sizes[j]
            # To make this weighting work, we "align" the boundary tokens against
            # every token in their sentence.
            if doc.tensor.sum() != wp_tensor.sum():
                for sent in doc.sents:
                    cls_vector = wp_tensor[sent._.pytt_start]
                    sep_vector = wp_tensor[sent._.pytt_end]
                    doc.tensor[sent.start : sent.end + 1] += cls_vector / len(sent)
                    doc.tensor[sent.start : sent.end + 1] += sep_vector / len(sent)
            doc.user_hooks["vector"] = get_doc_vector_via_tensor
            doc.user_span_hooks["vector"] = get_span_vector_via_tensor
            doc.user_token_hooks["vector"] = get_token_vector_via_tensor
            doc.user_hooks["similarity"] = get_similarity_via_tensor
            doc.user_span_hooks["similarity"] = get_similarity_via_tensor
            doc.user_token_hooks["similarity"] = get_similarity_via_tensor


def get_doc_vector_via_tensor(doc):
    return doc.tensor.sum(axis=0)


def get_span_vector_via_tensor(span):
    return span.doc.tensor[span.start : span.end].sum(axis=0)


def get_token_vector_via_tensor(token):
    return token.doc.tensor[token.i]


def get_similarity_via_tensor(doc1, doc2):
    v1 = doc1.vector
    v2 = doc2.vector
    xp = get_array_module(v1)
    return xp.dot(v1, v2) / (doc1.vector_norm * doc2.vector_norm)


@layerize
def get_word_pieces(sents, drop=0.0):
    assert isinstance(sents[0], Span)
    outputs = []
    for sent in sents:
        wordpieces = sent.doc._.pytt_word_pieces_
        wp_start = sent._.pytt_start
        wp_end = sent._.pytt_end
        if wp_start is not None and wp_end is not None:
            outputs.append(sent.doc._.pytt_word_pieces[wp_start : wp_end + 1])
        else:
            # Empty slice.
            outputs.append(sent.doc._.pytt_word_pieces[0:0])
    return outputs, None


@layerize
def get_last_hidden_state(activations, drop=0.0):
    def backprop_last_hidden_state(d_last_hidden_state, sgd=None):
        return d_last_hidden_state

    return activations.last_hidden_state, backprop_last_hidden_state


def without_length_batching(model: PyTT_Wrapper, _: Any) -> Model:
    Input = List[Array]
    Output = List[Activations]
    Backprop = Callable[[Output, Optional[Optimizer]], Optional[Input]]
    model_begin_update: Callable[[Array, Dropout], Tuple[Activations, Backprop]]
    model_begin_update = model.begin_update
 
    def apply_model_padded(inputs: Input, drop: Dropout=0.0) -> Tuple[Output, Backprop]:
        activs, get_dX = model_begin_update(pad_batch(inputs), drop)
        last_hiddens = [activs.last_hidden_state[i, : len(seq)]
                        for i, seq in enumerate(inputs)]
        outputs = [Activations(y, None, None, None) for y in last_hiddens]

        def backprop_batched(d_outputs, sgd=None):
            d_last_hiddens = [x.last_hidden_state for x in d_outputs]
            dY = pad_batch(d_last_hiddens)
            dY = dY.reshape(len(d_outputs), -1, dY.shape[-1])
            d_activs = Activations(dY, None, None, None, is_grad=True)
            dX = get_dX(d_activs, sgd=sgd)
            if dX is not None:
                d_inputs = [dX[i, : len(seq)] for i, seq in enumerate(d_outputs)]
            else:
                d_inputs = None
            return d_inputs

        return outputs, backprop_batched

    return wrap(apply_model_padded, model)


def with_length_batching(model: PyTT_Wrapper, min_batch: int) -> Model:
    """Wrapper that applies a model to variable-length sequences by first batching
    and padding the sequences. This allows us to group similarly-lengthed sequences
    together, making the padding less wasteful. If min_batch==1, no padding will
    be necessary.
    """
    if min_batch < 1:
        return without_length_batching(model, min_batch)

    Input = List[Array]
    Output = List[Activations]
    Backprop = Callable[[Output, Optional[Optimizer]], Input]
 
    def apply_model_to_batches(inputs: List[Array], drop: Dropout=0.0) -> Tuple[List[Activations], Backprop]:
        batches: List[List[int]] = batch_by_length(inputs, min_batch)
        # Initialize this, so we can place the outputs back in order.
        unbatched: List[Optional[Activations]] = [None for _ in inputs]
        backprops = []
        for indices in batches:
            X: Array = pad_batch([inputs[i] for i in indices])
            activs, get_dX = model.begin_update(X, drop=drop)
            backprops.append(get_dX)
            for i, j in enumerate(indices):
                unbatched[j] = activs.get_slice(i, slice(0, len(inputs[j])))
        outputs: List[Activations] = [y for y in unbatched if y is not None]
        assert len(outputs) == len(unbatched)

        def backprop_batched(d_outputs: Output, sgd: Optimizer=None) -> Input:
            d_inputs: List[Optional[Array]] = [None for _ in inputs]
            for indices, get_dX in zip(batches, backprops):
                d_activs = pad_batch_activations([d_outputs[i] for i in indices])
                dX = get_dX(d_activs, sgd=sgd)
                if dX is not None:
                    for i, j in enumerate(indices):
                        # As above, put things back in order, unpad.
                        d_inputs[j] = dX[i, : len(d_outputs[j])]
            not_none = [x for x in d_inputs if x is not None]
            assert len(not_none) == len(d_inputs)
            return not_none

        return outputs, backprop_batched

    return wrap(apply_model_to_batches, model)


def foreach_sentence(layer: Model, drop_factor: float=1.) -> PyTT_Wrapper:
    """Map a layer across sentences (assumes spaCy-esque .sents interface)"""
    ops = layer.ops

    Input = List[Doc]
    Output = List[Activations]
    Backprop = Callable[[Output, Optional[Optimizer]], None]
    def sentence_fwd(docs: Input, drop: Dropout = 0.0) -> Tuple[Output, Backprop]:
        sents: List[Span]
        sent_acts: List[Activations]
        bp_sent_acts: Callable[..., Optional[List[None]]]
        nested: List[List[Activations]]
        doc_sent_lengths: List[List[int]]
        doc_acts: List[Activations]
        
        sents = flatten_list([list(doc.sents) for doc in docs])
        sent_acts, bp_sent_acts = layer.begin_update(sents, drop=drop)
        # sent_acts is List[array], one per sent. Go to List[List[array]],
        # then List[array] (one per doc).
        nested = unflatten_list(sent_acts, [len(list(doc.sents)) for doc in docs])
        # Need this for the callback.
        doc_sent_lengths = [[len(sa) for sa in doc_sa] for doc_sa in nested]
        doc_acts = [Activations.join(doc_sa) for doc_sa in nested]
        assert len(docs) == len(doc_acts)

        def sentence_bwd(d_doc_acts: Output, sgd: Optional[Optimizer]=None) -> None:
            d_nested = [
                d_doc_acts[i].split(ops, doc_sent_lengths[i])
                for i in range(len(d_doc_acts))
            ]
            d_sent_acts = flatten_list(d_nested)
            d_ids = bp_sent_acts(d_sent_acts, sgd=sgd)
            if not (d_ids is None or all(ds is None for ds in d_ids)):
                raise ValueError("Expected gradient of sentence to be None")

        _assert_no_missing_doc_rows(docs, doc_acts)
        return doc_acts, sentence_bwd

    model = wrap(sentence_fwd, layer)
    return model


def _assert_no_missing_sent_rows(docs, sent_acts):
    total_rows = sum(sa.shape[0] for sa in sent_acts)
    total_wps = sum(len(doc._.pytt_word_pieces) for doc in docs)
    assert total_rows == total_wps, (total_rows, total_wps)
    for sent, sa in zip(sents, sent_acts):
        assert ((sent._.pytt_end+1)-sent._.pytt_start) == sa.shape[0], (sent._.pytt_start, sent._.pytt_end+1, sa.shape)

def _assert_no_missing_doc_rows(docs, doc_acts):
    assert len(docs) == len(doc_acts)
    for doc, da in zip(docs, doc_acts):
        shape = da.last_hidden_state.shape
        assert len(doc._.pytt_word_pieces) == shape[0], (len(doc._.pytt_word_pieces), shape[0])

