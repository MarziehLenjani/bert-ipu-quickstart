# Copyright 2019 Graphcore Ltd.
import argparse
import os
import logging
import numpy as np
import popart
import onnx
import re
import json
from typing import Dict
from onnx import numpy_helper
from logging import getLogger

from bert_model import BertConfig, Bert

logger = getLogger(__name__)


def get_tf_mapping(config):
    tf_to_onnx = {
        "bert/embeddings/word_embeddings": "Embedding/Embedding_Dict",
        "bert/embeddings/position_embeddings": "Embedding/Positional_Dict",
        "bert/embeddings/token_type_embeddings": "Embedding/Segment_Dict",
        "bert/embeddings/LayerNorm/gamma": "Embedding/Gamma",
        "bert/embeddings/LayerNorm/beta": "Embedding/Beta",
        "cls/predictions/transform/dense/kernel": "CLS/LMPredictionW",
        "cls/predictions/transform/dense/bias": "CLS/LMPredictionB",
        "cls/predictions/transform/LayerNorm/gamma": "CLS/Gamma",
        "cls/predictions/transform/LayerNorm/beta": "CLS/Beta"
    }
    for i in range(config.num_layers):
        layer = {
            f"bert/encoder/layer_{i}/attention/self/query/kernel": f"Layer{i}/Attention/QKV",
            f"bert/encoder/layer_{i}/attention/self/key/kernel": f"Layer{i}/Attention/QKV",
            f"bert/encoder/layer_{i}/attention/self/value/kernel": f"Layer{i}/Attention/QKV",
            f"bert/encoder/layer_{i}/attention/output/dense/kernel": f"Layer{i}/Attention/Out",
            f"bert/encoder/layer_{i}/attention/output/LayerNorm/gamma": f"Layer{i}/Attention/Gamma",
            f"bert/encoder/layer_{i}/attention/output/LayerNorm/beta": f"Layer{i}/Attention/Beta",
            f"bert/encoder/layer_{i}/intermediate/dense/kernel": f"Layer{i}/FF/1/W",
            f"bert/encoder/layer_{i}/intermediate/dense/bias": f"Layer{i}/FF/1/B",
            f"bert/encoder/layer_{i}/output/dense/kernel": f"Layer{i}/FF/2/W",
            f"bert/encoder/layer_{i}/output/dense/bias": f"Layer{i}/FF/2/B",
            f"bert/encoder/layer_{i}/output/LayerNorm/gamma": f"Layer{i}/FF/Gamma",
            f"bert/encoder/layer_{i}/output/LayerNorm/beta": f"Layer{i}/FF/Beta",
        }
        tf_to_onnx.update(**layer)
    return tf_to_onnx


def load_bert_config_tf(config_path):
    """
    Load the bert config data from Google Research's checkpoint format
    into the Popart Bert config format.
    """
    with open(config_path, "r") as fh:
        config_data = json.load(fh)

    config = BertConfig(
        vocab_length=config_data["vocab_size"],
        hidden_size=config_data["hidden_size"],
        sequence_length=config_data["max_position_embeddings"],
        max_positional_length=config_data["max_position_embeddings"],
        ff_size__=config_data["intermediate_size"],
        attention_heads=config_data["num_attention_heads"],
        num_layers=config_data["num_hidden_layers"],
        # TODO: Read the rest of these in from a GC config?
        projection_serialization_steps=2,
        batch_size=1,
        popart_dtype="FLOAT",
        no_dropout=True,
        custom_ops=["gather", "attention"]
    )

    return config


def generate_initializers(mapping, config, map_names, load_data):
    """
    Generate a graph initializer dictionary from the tensor names and
    data loaded from either a checkpoint or frozen graph using one of
    the methods below (`load_tf_ckpt_data` or `load_tf_frozen_data`).

    In the general case, this will simply map the tensor names from the
    TF model to the Popart model.

    The exception is the query-key-value matrix which is formed by
    concatenating the weight tensors Q, K and V.
    """
    initializers = {}
    qkv_tensor_range = {
        "query": (0, config.hidden_size),
        "key": (config.hidden_size, config.hidden_size * 2),
        "value": (config.hidden_size * 2, config.hidden_size * 3),
    }

    for name, array in zip(map_names, load_data):
        logger.debug(f"Initialising tensor from checkpoint {name} -> {mapping[name]}")

        if array.dtype == np.float32 and config.dtype == np.float16:
            array = array.astype(config.dtype)

        # If it's part of QKV, we need to handle separately as those 3
        # tensors need concatenating into one
        if mapping[name][-3:] == "QKV":
            qkv_part = name.split("/")[-2]

            if mapping[name] not in initializers.keys():
                qkv_shape = (array.shape[0], array.shape[1] * 3)
                initializers[mapping[name]] = np.empty(
                    qkv_shape, dtype=array.dtype
                )

            start_idx = qkv_tensor_range[qkv_part][0]
            end_idx = qkv_tensor_range[qkv_part][1]
            initializers[mapping[name]][:, start_idx:end_idx] = array
            logger.debug(f"Initialising QKV component {name}[{start_idx}:{end_idx}] from checkpoint")
            continue

        if mapping[name] == "Embedding/Embedding_Dict":
            tf_vocab_length = array.shape[0]
            diff = config.vocab_length - tf_vocab_length
            # Pad or Crop the vocab.
            if diff > 0:
                logger.debug(f"Padding the vocabulary. From {tf_vocab_length} to {config.vocab_length}")
                pad = np.zeros((diff, config.hidden_size)).astype(array.dtype)
                array = np.concatenate((array, pad), axis=0)
            else:
                logger.warn(f"Cropping the vocabulary may negatively effect performance. From {tf_vocab_length} to {config.vocab_length}")
                array = np.array(array[:config.vocab_length, :])
        if "gather" in config.custom_ops and mapping[name] in ["Embedding/Embedding_Dict", "Embedding/Positional_Dict"]:
            array = np.transpose(array)

        # FIXME: This copy is currently required to prevent popart misinterpreting the memory layout after the transpose.
        # Remove once T13187 is resolved.
        initializers[mapping[name]] = array.copy()
    return initializers


def load_tf_frozen_data(tf_frozen_path, mapping):
    """
    Parses a frozen-graph and outputs a tensors (lists of names and data) found
    in both the mapping and the checkpoint, ready for importing into the Bert
    model.
    """
    try:
        import tensorflow as tf
        from tensorflow.python.framework import tensor_util
    except ImportError:
        logger.error(
            "Loading a TensorFlow model requires TensorFlow to be installed. "
            "Please see https://www.tensorflow.org/install/ for installation "
            "instructions."
        )
        raise

    tf_path = os.path.abspath(tf_frozen_path)

    tf.reset_default_graph()
    with tf.io.gfile.GFile(tf_frozen_path, "rb") as f:
        graph_def = tf.GraphDef()
        graph_def.ParseFromString(f.read())

    # We'll search the graphdef for the nodes containing data we need to import
    map_names = [n.name for n in graph_def.node if n.name in mapping.keys()]
    load_data = [
        tensor_util.MakeNdarray(n.attr["value"].tensor)
        for n in graph_def.node
        if n.name in mapping.keys()
    ]

    return map_names, load_data


def load_tf_ckpt_data(tf_checkpoint_path, mapping):
    """
    Parses a checkpoint file and outputs a tensors (lists of names and data)
    found in both the mapping and the checkpoint, ready for importing into the
    Bert model.
    """
    try:
        import tensorflow as tf
    except ImportError:
        logger.error(
            "Loading a TensorFlow model requires TensorFlow to be installed. "
            "Please see https://www.tensorflow.org/install/ for installation "
            "instructions."
        )
        raise

    tf_path = os.path.abspath(tf_checkpoint_path)
    logger.info("Converting TensorFlow checkpoint from {}".format(tf_path))
    # Load weights from TF model
    init_vars = tf.train.list_variables(tf_path)

    map_names = [name for name, shape in init_vars if name in mapping.keys()]
    load_data = [tf.train.load_variable(tf_path, name) for name in map_names]

    return map_names, load_data


def load_initializers_from_tf(
    file_path,
    is_checkpoint,
    config,
):
    """
    Loads weights, etc. from Tensorflow files into a dictionary of Numpy Arrays.

    Can read either checkpoint files, or frozen graphs, according to the
    `is_checkpoint` flag, passed in as the second argument.
    """
    mapping = get_tf_mapping(config)

    if is_checkpoint:
        names, data = load_tf_ckpt_data(file_path, mapping)
    else:
        names, data = load_tf_frozen_data(file_path, mapping)

    return generate_initializers(mapping, config, names, data)


def load_model_from_tf(
    file_path,
    is_checkpoint,
    config,
    indices,
    positions,
    segments,
    builder=popart.Builder(),
):
    """
    Loads weights, etc. from Tensorflow files into the Graphcore IPU BERT
    implementation.

    Can read either checkpoint files, or frozen graphs, according to the
    `is_checkpoint` flag, passed in as the second argument.

    Requires input tensors to be provided to initialise the graph build.

    The user can optionally pass in a builder object (e.g. for compatibility
    with an older ONNX version). If not provided, a default builder is created.
    """
    initializers = load_initializers_from_tf(file_path, is_checkpoint, config)
    popart_model = Bert(config, builder=builder, initializers=initializers)

    output_tensor = popart_model.build_graph(indices, positions, segments)
    proto = builder.getModelProto()
    return popart_model, proto, output_tensor


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    # Required parameters
    group.add_argument(
        "--tf-checkpoint-path",
        default=None,
        type=str,
        help="Path to the TensorFlow checkpoint path.",
    )
    group.add_argument(
        "--tf-frozen-path",
        default=None,
        type=str,
        help="Path to the TensorFlow frozen graph (*.pb) path.",
    )
    parser.add_argument(
        "--bert-config-file",
        default=None,
        type=str,
        required=True,
        help="The config json file for the pre-trained BERT model.\n"
        "This specifies the model architecture.",
    )
    parser.add_argument(
        "--model-output-path",
        default=None,
        type=str,
        required=False,
        help="Path to the output PyTorch model.",
    )
    args = parser.parse_args()

    config = load_bert_config_tf(args.bert_config_file)

    # For now, the underlying model requires onnx9 slice, so a non-standard
    # builder is required.
    builder = popart.Builder(
        opsets={"ai.onnx": 9, "ai.onnx.ml": 1, "ai.graphcore": 1}
    )

    # Create the input tensors which will be needed to build the graph later
    sequence_info = popart.TensorInfo(
        "INT32", [config.batch_size * config.sequence_length]
    )

    indices = builder.addInputTensor(sequence_info)
    positions = builder.addInputTensor(sequence_info)
    segments = builder.addInputTensor(sequence_info)

    is_checkpoint = args.tf_checkpoint_path is not None
    input_filename = (
        args.tf_checkpoint_path if is_checkpoint else args.tf_frozen_path
    )

    popart_model, proto, output_tensor = load_model_from_tf(
        input_filename,
        is_checkpoint,
        config,
        indices,
        positions,
        segments,
        builder=builder,
    )

    logger.info("Graph parsed successfully.")

    if args.model_output_path is not None:
        onnx_proto = onnx.load_model_from_string(proto)
        onnx.save_model(onnx_proto, args.model_output_path)
