import torch
import numpy as np
from fairseq import utils, options, tasks
from fairseq.logging import progress_bar
from fairseq.dataclass.utils import convert_namespace_to_omegaconf
import sys
from pathlib import Path
import sys

import logging
from torchmetrics import functional as MF
import torch
import pandas as pd


def mean_frame(df_one):
    return df_one['y_pred'].mean()


def gen_result(df, test_id, func):
    pdbid, true, pred = [], [], []
    for id in test_id:
        res = df[df['pdbid'] == id]
        p = func(res)
        if p < 0: continue
        pdbid.append(id)
        true.append(res['y_true'].to_list()[0])
        pred.append(p)
    pdbid, true, pred = np.array(pdbid), np.array(true), np.array(pred)
    idx = true.argsort()
    pdbid, true, pred = pdbid[idx], true[idx], pred[idx]
    return pdbid, true, pred


def eval(args, cfg, task, model, checkpoint_path=None, logger=None, writer=None, step=0):
    # record
    save_file = checkpoint_path.parent / (checkpoint_path.name.split('.')[0] + f'{args.suffix}' + '.csv')
    # load checkpoint
    model_state = torch.load(checkpoint_path)["model"]

    model.load_state_dict(
        model_state, strict=True, model_cfg=cfg.model
    )
    del model_state
    model.to(torch.cuda.current_device())
    # load dataset
    split = args.split
    task.load_dataset(split)
    batch_iterator = task.get_batch_iterator(
        dataset=task.dataset(split),
        max_tokens=cfg.dataset.max_tokens_valid,
        max_sentences=cfg.dataset.batch_size_valid,
        max_positions=utils.resolve_max_positions(
            task.max_positions(),
            model.max_positions(),
        ),
        ignore_invalid_inputs=cfg.dataset.skip_invalid_size_inputs_valid_test,
        required_batch_size_multiple=cfg.dataset.required_batch_size_multiple,
        seed=cfg.common.seed,
        num_workers=cfg.dataset.num_workers,
        epoch=0,
        data_buffer_size=cfg.dataset.data_buffer_size,
        disable_iterator_cache=False,
    )
    itr = batch_iterator.next_epoch_itr(
        shuffle=False, set_dataset_epoch=False
    )
    progress = progress_bar.progress_bar(
        itr,
        log_format=cfg.common.log_format,
        log_interval=cfg.common.log_interval,
    )

    # infer
    y_pred = []
    y_true = []
    pdbid, frames = [], []
    weights = []
    with torch.no_grad():
        model.eval()
        for i, sample in enumerate(progress):
            sample = utils.move_to_cuda(sample)
            y = model(**sample["net_input"])
            if isinstance(y, tuple):
                y, weight = y[0], y[1]
            else:
                y, weight = y, torch.ones(y.shape, device=y.device, dtype=y.dtype)
            y = y.reshape(-1)
            weight = weight.reshape(-1)

            weights.extend(weight.detach().cpu())
            y_pred.extend(y.detach().cpu())
            y_true.extend(sample["net_input"]["batched_data"]['y'].cpu().reshape(-1))
            pdbid.extend(sample["net_input"]["batched_data"]['pdbid'])
            frames.extend(sample["net_input"]["batched_data"]['frame'])

            torch.cuda.empty_cache()

    # save predictions
    y_pred = torch.Tensor(y_pred)
    y_true = torch.Tensor(y_true)
    weights = torch.Tensor(weights)
    frames = torch.Tensor(frames)
    pdbid = pdbid
    assert len(pdbid) == len(frames)
    print(len(pdbid), len(frames), len(y_pred), len(y_true), len(weights))

    y_pred = y_pred * 1.9919705951218716 + 6.529300030461668

    # evaluate pretrained models

    print(f"save results to {save_file}")
    df = pd.DataFrame({
        "pdbid": pdbid,
        "frame": frames,
        "y_true": y_true.to(torch.float32).cpu().numpy(),
        "y_pred": y_pred.to(torch.float32).cpu().numpy(),
        "weight": weights
    })
    df.to_csv(save_file, index=False)

    single_pdbid, single_true, single_pred = gen_result(df, df['pdbid'], mean_frame)

    y_true, y_pred = torch.from_numpy(single_true), torch.from_numpy(single_pred)

    r = MF.pearson_corrcoef(y_pred.to(torch.float32), y_true.to(torch.float32))
    logger.info(f"pearson_r: {r}")
    r = MF.r2_score(y_pred.to(torch.float32), y_true.to(torch.float32))
    logger.info(f"r2: {r}")
    mae = np.mean(np.abs(y_true.numpy() - y_pred.numpy()))
    logger.info(f"mae: {mae}")
    mse = MF.mean_squared_error(y_pred.to(torch.float32), y_true.to(torch.float32))
    logger.info(f"mse: {mse}")
    r = MF.mean_absolute_percentage_error(y_pred.to(torch.float32), y_true.to(torch.float32))
    logger.info(f"mape: {r}")
    r = MF.symmetric_mean_absolute_percentage_error(y_pred.to(torch.float32), y_true.to(torch.float32))
    logger.info(f"smape: {r}")

def main():
    parser = options.get_training_parser()
    parser.add_argument(
        "--split",
        type=str,
    )
    parser.add_argument(
        "--suffix",
        type=str
    )

    args = options.parse_args_and_arch(parser, modify_parser=None)
    logger = logging.getLogger(__name__)
    cfg = convert_namespace_to_omegaconf(args)
    np.random.seed(cfg.common.seed)
    utils.set_torch_seed(cfg.common.seed)
    # initialize task
    task = tasks.setup_task(cfg.task)
    model = task.build_model(cfg.model)

    if hasattr(args, "save_dir"):
        # eval(args, cfg, task, model, Path(args.save_dir) / "checkpoint_best.pt", logger, None, -1)
        # eval(args, cfg, task, model, Path(args.save_dir) / "checkpoint_last.pt", logger, None, 0)
        pathes = []
        for checkpoint_path in Path(args.save_dir).glob("checkpoint[123456789]*.pt"):
            epoch = checkpoint_path.name.split('.')[0].removeprefix('checkpoint')
            pathes.append((epoch, checkpoint_path))
        print(pathes)
        pathes = sorted(pathes, key=lambda x: x[0], reverse=True)
        for epoch, checkpoint_path in pathes[:10]:
            logger.info(f"evaluating checkpoint file {checkpoint_path}")
            try:
                eval(args, cfg, task, model, checkpoint_path, logger, None, epoch)
            except:
                pass
            sys.stdout.flush()

        
if __name__ == '__main__':
    main()