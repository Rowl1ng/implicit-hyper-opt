import os
import time

import ipdb
import argparse
from tqdm import tqdm
import numpy as np
import torch
import torch.optim as optim
import torch.nn.functional as F
from torch.autograd import grad
from torch.autograd import Variable
from torch.optim.lr_scheduler import MultiStepLR

# Local imports
import data_loaders
from models.unet import UNet
from models import resnet_cifar
from models.resnet import ResNet18
from models.simple_models import Net
from models.wide_resnet import WideResNet
from utils.util import gather_flat_grad


def saver(epoch, elementary_model, elementary_optimizer, augment_net, reweighting_net, hyper_optimizer, path):
    """

    :param epoch:
    :param elementary_model:
    :param elementary_optimizer:
    :param augment_net:
    :param reweighting_net:
    :param hyper_optimizer:
    :param path:
    :return:
    """
    torch.save({
        'epoch': epoch,
        'elementary_model_state_dict': elementary_model.state_dict(),
        'elementary_optimizer_state_dict': elementary_optimizer.state_dict(),
        'augment_model_state_dict': augment_net.state_dict(),
        'reweighting_net_state_dict': reweighting_net.state_dict(),
        'hyper_optimizer_state_dict': hyper_optimizer.state_dict()
    }, path + '/checkpoint.pt')


def load_baseline_model(args):
    """

    :param args:
    :return:
    """
    if args.dataset == 'cifar10':
        num_classes = 10
        train_loader, val_loader, test_loader = data_loaders.load_cifar10(args.batch_size, val_split=True,
                                                                          augmentation=args.data_augmentation)
    elif args.dataset == 'cifar100':
        num_classes = 100
        train_loader, val_loader, test_loader = data_loaders.load_cifar100(args.batch_size, val_split=True,
                                                                           augmentation=args.data_augmentation)
    elif args.dataset == 'mnist':
        args.datasize, args.valsize, args.testsize = 100, 100, 100
        num_train = args.datasize
        if args.datasize == -1:
            num_train = 50000

        from data_loaders import load_mnist
        train_loader, val_loader, test_loader = load_mnist(args.batch_size,
                                                           subset=[args.datasize, args.valsize, args.testsize],
                                                           num_train=num_train)

    if args.model == 'resnet18':
        cnn = ResNet18(num_classes=num_classes)
    elif args.model == 'wideresnet':
        cnn = WideResNet(depth=28, num_classes=num_classes, widen_factor=10, dropRate=0.3)

    checkpoint = None
    if args.load_baseline_checkpoint:
        checkpoint = torch.load(args.load_baseline_checkpoint)
        cnn.load_state_dict(checkpoint['model_state_dict'])

    model = cnn.cuda()
    model.train()
    return model, train_loader, val_loader, test_loader, checkpoint


def load_finetuned_model(args, baseline_model):
    """

    :param args:
    :param baseline_model:
    :return:
    """
    # augment_net = Net(0, 0.0, 32, 3, 0.0, num_classes=32**2 * 3, do_res=True)
    augment_net = UNet(in_channels=3, n_classes=3, depth=1, wf=2, padding=True, batch_norm=False,
                       do_noise_channel=True,
                       up_mode='upsample', use_identity_residual=True)  # TODO(PV): Initialize UNet properly
    # TODO (JON): DEPTH 1 WORKED WELL.  Changed upconv to upsample.  Use a wf of 2.

    # This ResNet outputs scalar weights to be applied element-wise to the per-example losses
    from models.simple_models import CNN, Net
    imsize, in_channel, num_classes = 32, 3, 10
    reweighting_net = Net(0, 0.0, imsize, in_channel, 0.0, num_classes=1)
    #resnet_cifar.resnet20(num_classes=1)

    if args.load_finetune_checkpoint:
        checkpoint = torch.load(args.load_finetune_checkpoint)
        baseline_model.load_state_dict(checkpoint['elementary_model_state_dict'])
        augment_net.load_state_dict(checkpoint['augment_model_state_dict'])
        try:
            reweighting_net.load_state_dict(checkpoint['reweighting_model_state_dict'])
        except KeyError:
            pass

    augment_net, reweighting_net, baseline_model = augment_net.cuda(), reweighting_net.cuda(), baseline_model.cuda()
    augment_net.train(), reweighting_net.train(), baseline_model.train()
    return augment_net, reweighting_net, baseline_model


def zero_hypergrad(get_hyper_train):
    """

    :param get_hyper_train:
    :return:
    """
    current_index = 0
    for p in get_hyper_train():
        p_num_params = np.prod(p.shape)
        if p.grad is not None:
            p.grad = p.grad * 0
        current_index += p_num_params


def store_hypergrad(get_hyper_train, total_d_val_loss_d_lambda):
    """

    :param get_hyper_train:
    :param total_d_val_loss_d_lambda:
    :return:
    """
    current_index = 0
    for p in get_hyper_train():
        p_num_params = np.prod(p.shape)
        p.grad = total_d_val_loss_d_lambda[current_index:current_index + p_num_params].view(p.shape)
        current_index += p_num_params


def neumann_hyperstep_preconditioner(d_val_loss_d_theta, d_train_loss_d_w, elementary_lr, num_neumann_terms):
    preconditioner = d_val_loss_d_theta.detach()
    counter = preconditioner
    old_size = torch.sum(counter ** 2)
    print(f"term {-1}: size = {torch.sum(preconditioner ** 2)}")

    # Do the fixed point iteration to approximate the vector-inverseHessian product
    for i in range(num_neumann_terms):
        old_counter = counter

        # This increments counter to counter * (I - hessian) = counter - counter * hessian
        hessian_term = (counter.view(1, -1) @ d_train_loss_d_w.view(-1, 1) @ d_train_loss_d_w.view(1, -1)).view(-1)
        counter = counter - elementary_lr * hessian_term

        size, diff = torch.sum(counter ** 2), torch.sum((counter - old_counter) ** 2)
        rel_change = size / old_size
        print(f"term {i}: size = {size}, rel_change = {rel_change}, diff={diff}")
        if rel_change > 0.9999: break

        preconditioner = preconditioner + counter
        old_size = size
    return preconditioner


def cg_batch(A_bmm, B, M_bmm=None, X0=None, rtol=1e-4, atol=0.0, maxiter=5, verbose=True):
    """Solves a batch of PD matrix linear systems using the preconditioned CG algorithm.

    This function solves a batch of matrix linear systems of the form

        A_i X_i = B_i,  i=1,...,K,

    where A_i is a n x n positive definite matrix and B_i is a n x m matrix,
    and X_i is the n x m matrix representing the solution for the ith system.

    Args:
        A_bmm: A callable that performs a batch matrix multiply of A and a K x n x m matrix.
        B: A K x n x m matrix representing the right hand sides.
        M_bmm: (optional) A callable that performs a batch matrix multiply of the preconditioning
            matrices M and a K x n x m matrix. (default=identity matrix)
        X0: (optional) Initial guess for X, defaults to M_bmm(B). (default=None)
        rtol: (optional) Relative tolerance for norm of residual. (default=1e-3)
        atol: (optional) Absolute tolerance for norm of residual. (default=0)
        maxiter: (optional) Maximum number of iterations to perform. (default=5*n)
        verbose: (optional) Whether or not to print status messages. (default=False)
    """
    K, n, m = B.shape

    if M_bmm is None:
        M_bmm = lambda x: x
    if X0 is None:
        X0 = M_bmm(B)
    if maxiter is None:
        maxiter = 5 * n

    assert B.shape == (K, n, m)
    assert X0.shape == (K, n, m)
    assert rtol > 0 or atol > 0
    assert isinstance(maxiter, int)

    X_k = X0
    R_k = B - A_bmm(X_k)
    Z_k = M_bmm(R_k)

    P_k = torch.zeros_like(Z_k)

    P_k1 = P_k
    R_k1 = R_k
    R_k2 = R_k
    X_k1 = X0
    Z_k1 = Z_k
    Z_k2 = Z_k

    B_norm = torch.norm(B, dim=1)
    stopping_matrix = torch.max(rtol * B_norm, atol * torch.ones_like(B_norm))

    if verbose:
        residual_norm = torch.norm(A_bmm(X_k) - B, dim=1)
        print("%03s | %010s %06s" % ("it", torch.max(residual_norm - stopping_matrix), "it/s"))

    optimal = False
    start = time.perf_counter()
    cur_error = 1e-8
    epsilon = 1e-10
    for k in range(1, maxiter + 1):
        # epsilon = cur_error ** 3  # 1e-8

        start_iter = time.perf_counter()
        Z_k = M_bmm(R_k)

        if k == 1:
            P_k = Z_k
            R_k1 = R_k
            X_k1 = X_k
            Z_k1 = Z_k
        else:
            R_k2 = R_k1
            Z_k2 = Z_k1
            P_k1 = P_k
            R_k1 = R_k
            Z_k1 = Z_k
            X_k1 = X_k
            denominator = (R_k2 * Z_k2).sum(1)
            denominator[denominator < epsilon / 2] = epsilon  # epsilon
            beta = (R_k1 * Z_k1).sum(1) / denominator
            P_k = Z_k1 + beta.unsqueeze(1) * P_k1

        denominator = (P_k * A_bmm(P_k)).sum(1)
        denominator[denominator < epsilon / 2] = epsilon
        alpha = (R_k1 * Z_k1).sum(1) / denominator
        X_k = X_k1 + alpha.unsqueeze(1) * P_k
        R_k = R_k1 - alpha.unsqueeze(1) * A_bmm(P_k)
        end_iter = time.perf_counter()

        residual_norm = torch.norm(A_bmm(X_k) - B, dim=1)

        cur_error = torch.max(residual_norm - stopping_matrix)
        if verbose:
            print("%03d | %8.6e %4.2f" %
                  (k, cur_error,
                   1. / (end_iter - start_iter)))

        if (residual_norm <= stopping_matrix).all():
            optimal = True
            break

    end = time.perf_counter()

    if verbose:
        if optimal:
            print("Terminated in %d steps (optimal). Took %.3f ms." %
                  (k, (end - start) * 1000))
        else:
            print("Terminated in %d steps (reached maxiter). Took %.3f ms." %
                  (k, (end - start) * 1000))

    info = {
        "niter": k,
        "optimal": optimal
    }

    return X_k, info


# TODO: Get rid of iterating over loader.  Just sample the 'next' one.
# TODO: Don't feed in the grad.  Recompute it
# TODO: Dont give the elementary optimizer... Just the lr?
# TODO: Take the hyper_step outside of this so I dont feed in optimizer
def hyper_step(get_hyper_train, model, val_loss_func, val_loader, d_train_loss_d_w, elementary_lr, use_reg, args):
    """Estimate the hypergradient, and take an update with it.

    :param get_hyper_train:  A function which returns the hyperparameters we want to tune.
    :param model:  A function which returns the elementary parameters we want to tune.
    :param val_loss_func:  A function which takes input x and output y, then returns the scalar valued loss.
    :param val_loader: A generator for input x, output y tuples.
    :param d_train_loss_d_w:  The derivative of the training loss with respect to elementary parameters.
    :param hyper_optimizer: The optimizer which updates the hyperparameters.
    :return: The scalar valued validation loss, the hyperparameter norm, and the hypergradient norm.
    """
    zero_hypergrad(get_hyper_train)

    d_train_loss_d_w = gather_flat_grad(d_train_loss_d_w)

    # Compute gradients of the validation loss w.r.t. the weights/hypers
    num_weights, num_hypers = sum(p.numel() for p in model.parameters()), sum(p.numel() for p in get_hyper_train())
    d_val_loss_d_theta, direct_grad = torch.zeros(num_weights).cuda(), torch.zeros(num_hypers).cuda()
    model.train(), model.zero_grad()
    for batch_idx, (x, y) in enumerate(val_loader):
        val_loss = val_loss_func(x, y)
        d_val_loss_d_theta += gather_flat_grad(grad(val_loss, model.parameters(), retain_graph=use_reg))
        if use_reg:
            direct_grad += gather_flat_grad(grad(val_loss, get_hyper_train()))
            direct_grad[direct_grad != direct_grad] = 0
        break

    # Initialize the preconditioner and counter
    if not args.use_cg:
        preconditioner = neumann_hyperstep_preconditioner(d_val_loss_d_theta, d_train_loss_d_w,
                                                          elementary_lr, args.num_neumann_terms)
    else:
        def A_vector_multiply_func(vec):
            p1 = d_val_loss_d_theta.view(1, -1) @ vec.view(-1, 1)
            p2 = d_val_loss_d_theta.view(-1, 1) @ p1
            return p2.view(1, -1, 1)

        preconditioner, _ = cg_batch(A_vector_multiply_func, d_val_loss_d_theta.view(1, -1, 1))
    # conjugate_grad(A_vector_multiply_func, d_val_loss_d_theta)

    # compute d / d lambda (partial Lv / partial w * partial Lt / partial w)
    # = (partial Lv / partial w * partial^2 Lt / (partial w partial lambda))
    indirect_grad = gather_flat_grad(grad(d_train_loss_d_w, get_hyper_train(), grad_outputs=preconditioner.view(-1)))
    hypergrad = direct_grad + indirect_grad

    store_hypergrad(get_hyper_train, hypergrad)
    return val_loss, hypergrad.norm()


def get_models(args):
    model, train_loader, val_loader, test_loader, checkpoint = load_baseline_model(args)
    augment_net, reweighting_net, model = load_finetuned_model(args, model)
    return model, train_loader, val_loader, test_loader, augment_net, reweighting_net, checkpoint


def experiment(args):
    # Setup the random seeds
    torch.manual_seed(args.seed)
    np.random.seed(args.seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed(args.seed)

    # Load the baseline model
    args.load_baseline_checkpoint = '/h/lorraine/PycharmProjects/CG_IFT_test/baseline_checkpoints/cifar10_resnet18_sgdm_lr0.1_wd0.0005_aug1.pt'
    args.load_finetune_checkpoint = None  # TODO: Make it load the augment net if this is provided
    model, train_loader, val_loader, test_loader, augment_net, reweighting_net, checkpoint = get_models(args)

    # Load the logger
    from train_augment_net_multiple import load_logger, get_id
    csv_logger, test_id = load_logger(args)
    args.save_loc = './finetuned_checkpoints/' + get_id(args)

    # Hyperparameter access functions
    def get_hyper_train():
        # return torch.cat([p.view(-1) for p in augment_net.parameters()])
        if args.use_augment_net and args.use_reweighting_net:
            return list(augment_net.parameters()) + list(reweighting_net.parameters())
        elif args.use_augment_net:
            return augment_net.parameters()
        elif args.use_reweighting_net:
            return reweighting_net.parameters()

    def get_hyper_train_flat():
        if args.use_augment_net and args.use_reweighting_net:
            return torch.cat([torch.cat([p.view(-1) for p in augment_net.parameters()]),
                              torch.cat([p.view(-1) for p in reweighting_net.parameters()])])
        elif args.use_reweighting_net:
            return torch.cat([p.view(-1) for p in reweighting_net.parameters()])
        elif args.use_augment_net:
            return torch.cat([p.view(-1) for p in augment_net.parameters()])

    # Setup the optimizers
    if args.load_baseline_checkpoint is not None:
        args.lr = args.lr * 0.2 * 0.2 * 0.2
    optimizer = optim.SGD(model.parameters(), lr=args.lr, momentum=0.9, nesterov=True, weight_decay=args.wdecay)
    scheduler = MultiStepLR(optimizer, milestones=[60, 120, 160], gamma=0.2)  # [60, 120, 160]
    # optimizer.load_state_dict(checkpoint['optimizer_state_dict'])
    hyper_optimizer = optim.Adam(get_hyper_train(), lr=1e-3)  # Adam(get_hyper_train())
    hyper_scheduler = MultiStepLR(hyper_optimizer, milestones=[40, 100, 140], gamma=0.2)

    graph_iter = 0
    def train_loss_func(x, y):
        x, y = x.cuda(), y.cuda()
        reg = 0.

        if args.use_augment_net:
            # old_x = x
            x = augment_net(x, class_label=y)
            '''num_sample = 10
            xs =torch.zeros(num_sample, x.shape[0], x.shape[1], x.shape[2], x.shape[3]).cuda()
            for i in range(num_sample):
                xs[i] = augment_net(x, class_label=y)
            xs_diffs = (torch.mean(xs, dim=0) - old_x) ** 2
            diff_loss = torch.mean(xs_diffs)
            entrop_loss = -torch.mean(torch.std(xs, dim=0) ** 2)
            reg = 10 * diff_loss + entrop_loss'''

        pred = model(x)
        xentropy_loss = F.cross_entropy(pred, y, reduction='none')

        if args.use_reweighting_net:
            loss_weights = reweighting_net(x)  # TODO: Or reweighting_net(augment_x) ??
            loss_weights = loss_weights.squeeze()
            loss_weights = F.sigmoid(loss_weights / 10.0 ) * 2.0 + 0.1
            # loss_weights = (loss_weights - torch.mean(loss_weights)) / torch.std(loss_weights)
            # loss_weights = F.softmax(loss_weights)
            # loss_weights = loss_weights * args.batch_size
            # TODO: Want loss_weight vs x_entropy_loss

            nonlocal graph_iter
            if graph_iter % 100 == 0:
                import matplotlib.pyplot as plt
                np_loss = xentropy_loss.data.cpu().numpy()
                np_weight = loss_weights.data.cpu().numpy()
                for i in range(10):
                    class_indices = (y == i).cpu().numpy()
                    class_indices = [val*ind for val, ind in enumerate(class_indices) if val != 0]
                    plt.scatter(np_loss[class_indices], np_weight[class_indices], alpha=0.5, label=str(i))
                # plt.scatter((xentropy_loss*loss_weights).data.cpu().numpy(), loss_weights.data.cpu().numpy(), alpha=0.5, label='weighted')
                # print(np_loss)
                plt.ylim([np.min(np_weight) / 2.0, np.max(np_weight) * 2.0])
                plt.xlim([np.min(np_loss) / 2.0, np.max(np_loss) * 2.0])
                plt.yscale('log')
                plt.xscale('log')
                plt.axhline(1.0, c='k')
                plt.ylabel("loss_weights")
                plt.xlabel("xentropy_loss")
                plt.legend()
                plt.savefig("images/aaaa_lossWeightvsEntropy.pdf")
                plt.clf()

            xentropy_loss = xentropy_loss * loss_weights
        graph_iter += 1

        xentropy_loss = xentropy_loss.mean() + reg
        return xentropy_loss, pred

    use_reg = args.use_augment_net
    reg_anneal_epoch = 0
    stop_reg_epoch = 200
    if args.reg_weight == 0:
        use_reg = False

    def val_loss_func(x, y):
        x, y = x.cuda(), y.cuda()
        pred = model(x)
        xentropy_loss = F.cross_entropy(pred, y)

        reg = 0
        if args.use_augment_net:
            if use_reg:
                num_sample = 10
                xs = torch.zeros(num_sample, x.shape[0], x.shape[1], x.shape[2], x.shape[3]).cuda()
                for i in range(num_sample):
                    xs[i] = augment_net(x, class_label=y)
                xs_diffs = (torch.abs(torch.mean(xs, dim=0) - x))
                diff_loss = torch.mean(xs_diffs)
                stds = torch.std(xs, dim=0)
                entrop_loss = -torch.mean(stds)
                # TODO : Remember to add direct grad back in to hyper_step
                reg = args.reg_weight * (diff_loss + entrop_loss)
            else:
                reg = 0

        # reg *= (args.num_finetune_epochs - reg_anneal_epoch) / (args.num_finetune_epochs + 2)
        if reg_anneal_epoch >= stop_reg_epoch:
            reg *= 0
        return xentropy_loss + reg

    def test(loader, do_test_augment=True, num_augment=10):
        model.eval()  # Change model to 'eval' mode (BN uses moving mean/var).
        correct, total = 0., 0.
        losses = []
        for images, labels in loader:
            images, labels = images.cuda(), labels.cuda()

            with torch.no_grad():
                pred = model(images)
                if do_test_augment:
                    if args.use_augment_net and args.num_neumann_terms >= 0:
                        shape_0, shape_1 = pred.shape[0], pred.shape[1]
                        pred = pred.view(1, shape_0, shape_1)  # Batch size, num_classes
                        for _ in range(num_augment):
                            pred = torch.cat((pred, model(augment_net(images)).view(1, shape_0, shape_1)))
                        pred = torch.mean(pred, dim=0)
                xentropy_loss = F.cross_entropy(pred, labels)
                losses.append(xentropy_loss.item())

            pred = torch.max(pred.data, 1)[1]
            total += labels.size(0)
            correct += (pred == labels).sum().item()

        avg_loss = float(np.mean(losses))
        acc = correct / total
        model.train()
        return avg_loss, acc

    # print(f"Initial Val Loss: {test(val_loader)}")
    # print(f"Initial Test Loss: {test(test_loader)}")

    init_time = time.time()
    val_loss, val_acc = test(val_loader)
    test_loss, test_acc = test(test_loader)
    print(f"Initial Val Loss: {val_loss, val_acc}")
    print(f"Initial Test Loss: {test_loss, test_acc}")
    iteration = 0
    for epoch in range(0, args.num_finetune_epochs):
        reg_anneal_epoch = epoch
        xentropy_loss_avg = 0.
        total_val_loss, val_loss = 0., 0.
        correct = 0.
        total = 0.
        weight_norm, grad_norm = .0, .0

        progress_bar = tqdm(train_loader)
        num_tune_hyper = 45000 / 5000  # 1/5th the val data as train data
        hyper_num = 0
        for i, (images, labels) in enumerate(progress_bar):
            progress_bar.set_description('Finetune Epoch ' + str(epoch))

            images, labels = images.cuda(), labels.cuda()
            # pred = model(images)
            xentropy_loss, pred = train_loss_func(images, labels)  # F.cross_entropy(pred, labels)
            xentropy_loss_avg += xentropy_loss.item()

            current_index = 0
            for p in model.parameters():
                p_num_params = np.prod(p.shape)
                if p.grad is not None:
                    p.grad = p.grad * 0
                current_index += p_num_params
            # optimizer.zero_grad()
            train_grad = grad(xentropy_loss, model.parameters(), create_graph=True)  #

            if args.num_neumann_terms >= 0:  # if this is less than 0, then don't do hyper_steps
                if i % num_tune_hyper == 0:
                    cur_lr = 1.0
                    for param_group in optimizer.param_groups:
                        cur_lr = param_group['lr']
                        break
                    val_loss, grad_norm = hyper_step(get_hyper_train, model, val_loss_func, val_loader,
                                                     train_grad, cur_lr, use_reg, args)
                    hyper_optimizer.step()

                    weight_norm = get_hyper_train_flat().norm()
                    total_val_loss += val_loss.item()
                    hyper_num += 1

            # Replace the original gradient for the elementary optimizer step.
            current_index = 0
            flat_train_grad = gather_flat_grad(train_grad)
            for p in model.parameters():
                p_num_params = np.prod(p.shape)
                # if p.grad is not None:
                p.grad = flat_train_grad[current_index: current_index + p_num_params].view(p.shape)
                current_index += p_num_params
            optimizer.step()

            iteration += 1

            # Calculate running average of accuracy
            pred = torch.max(pred.data, 1)[1]
            total += labels.size(0)
            correct += (pred == labels.data).sum().item()
            accuracy = correct / total

            progress_bar.set_postfix(
                train='%.4f' % (xentropy_loss_avg / (i + 1)),
                val='%.4f' % (total_val_loss / max(hyper_num, 1)),
                acc='%.4f' % accuracy,
                weight='%.3f' % weight_norm,
                update='%.3f' % grad_norm
            )
            if i % (num_tune_hyper ** 2) == 0 and args.use_augment_net:
                from train_augment_net_graph import save_images
                if args.do_diagnostic:
                    save_images(images, labels, augment_net, args)
                saver(epoch, model, optimizer, augment_net, reweighting_net, hyper_optimizer, args.save_loc)
                val_loss, val_acc = test(val_loader)
                csv_logger.writerow({'epoch': str(epoch),
                                     'train_loss': str(xentropy_loss_avg / (i + 1)), 'train_acc': str(accuracy),
                                     'val_loss': str(val_loss), 'val_acc': str(val_acc),
                                     'test_loss': str(test_loss), 'test_acc': str(test_acc),
                                     'run_time': time.time() - init_time,
                                     'iteration': iteration})

        val_loss, val_acc = test(val_loader)
        test_loss, test_acc = test(test_loader)
        tqdm.write('val loss: {:6.4f} | val acc: {:6.4f} | test loss: {:6.4f} | test_acc: {:6.4f}'.format(
            val_loss, val_acc, test_loss, test_acc))

        scheduler.step(epoch)  # , hyper_scheduler.step(epoch)
        csv_logger.writerow({'epoch': str(epoch),
                             'train_loss': str(xentropy_loss_avg / (i + 1)), 'train_acc': str(accuracy),
                             'val_loss': str(val_loss), 'val_acc': str(val_acc),
                             'test_loss': str(test_loss), 'test_acc': str(test_acc),
                             'run_time': time.time() - init_time, 'iteration': iteration})


def make_test_arg():
    from train_augment_net_multiple import make_parser, make_argss

    test_args = make_parser().parse_args()  # make_argss()[0]
    test_args.reg_weight = .5
    # TODO: What am I tuning?

    test_args.seed = 7777
    test_args.do_diagnostic = True
    test_args.data_augmentation = False
    test_args.use_reweighting_net = False
    test_args.use_augment_net = False
    test_args.dataset = 'mnist'  # TODO: Need to add dataset to the save info?

    # TODO: Change the inversion strategies
    test_args.num_neumann_terms = 0
    test_args.use_cg = True
    return test_args


if __name__ == '__main__':
    # TODO: Need to make a separate arg for 0, I, CG, neumann
    experiment(make_test_arg())
