from __future__ import absolute_import, division, print_function
import numpy as np
import tensorflow as tf
from config import get_config
from data_input import DataInput
from utils.callbacks import TensorBoard
from models.motiongan import MotionGANV1, MotionGANV2, MotionGANV3, MotionGANV4
from utils.restore_keras_model import restore_keras_model
from tqdm import trange
from utils.viz import plot_seq_gif, plot_seq_emb
from tensorflow.python.platform import tf_logging as logging

logging = tf.logging
flags = tf.flags
flags.DEFINE_bool("verbose", False, "To talk or not to talk")
flags.DEFINE_string("save_path", None, "Model output directory")
flags.DEFINE_string("config_file", None, "Model config file")
FLAGS = flags.FLAGS

if __name__ == "__main__":
    if not tf.gfile.Exists('./save'):
        tf.gfile.MkDir('./save')

    # Config stuff
    config = get_config(FLAGS)

    data_input = DataInput(config)
    train_batches = data_input.train_epoch_size * config.epoch_factor
    train_generator = data_input.batch_generator(True)
    val_batches = data_input.val_epoch_size
    val_generator = data_input.batch_generator(False)

    # Model building
    if config.model_type == 'motiongan':
        if config.model_version == 'v1':
            model_wrap = MotionGANV1(config)
        if config.model_version == 'v2':
            model_wrap = MotionGANV2(config)
        if config.model_version == 'v3':
            model_wrap = MotionGANV3(config)
        if config.model_version == 'v4':
            model_wrap = MotionGANV4(config)

    if FLAGS.verbose:
        print('Discriminator model:')
        print(model_wrap.disc_model.summary())
        print('Generator model:')
        print(model_wrap.gen_model.summary())
        print('GAN model:')
        print(model_wrap.gan_model.summary())

    if config.epoch > 0:
        model_wrap.disc_model = restore_keras_model(
            model_wrap.disc_model, config.save_path + '_disc_weights.hdf5', False)
        model_wrap.gen_model = restore_keras_model(
            model_wrap.gen_model, config.save_path + '_gen_weights.hdf5', False)

    tensorboard = TensorBoard(log_dir=config.save_path + '_logs',
                              epoch=config.epoch,
                              n_batches=train_batches,
                              batch_size=config.batch_size,
                              write_graph=True)
    tensorboard.set_model(model_wrap.gan_model)

    mask_modes = ('No mask', 'Future Prediction', 'Occlusion Simulation', 'Structured Occlusion', 'Noisy Transmission')

    def gen_mask(mask_type=0, keep_prob=1.0):
        # Default mask, no mask
        mask = np.ones(shape=(config.batch_size, config.njoints, model_wrap.seq_len, 1))
        if mask_type == 1:  # Future Prediction
            mask[:, :, np.int(model_wrap.seq_len * keep_prob):, :] = 0.0
        elif mask_type == 2:  # Occlusion Simulation
            rand_joints = np.random.randint(config.njoints, size=np.int(config.njoints * (1.0 - keep_prob)))
            mask[:, rand_joints, :, :] = 0.0
        elif mask_type == 3:  # Structured Occlusion Simulation
            rand_joints = set()
            while ((config.njoints - len(rand_joints)) >
                   (config.njoints * keep_prob)):
                joints_to_add = (config.body_members.values()[np.random.randint(len(config.body_members))])['joints']
                for joint in joints_to_add:
                    rand_joints.add(joint)
            mask[:, list(rand_joints), :, :] = 0.0
        elif mask_type == 4:  # Noisy transmission
            mask = np.random.binomial(1, keep_prob, size=mask.shape)

        return mask

    def gen_latent_noise():
        return np.random.uniform(size=(config.batch_size, config.latent_cond_dim))

    def save_models():
        # logging.set_verbosity(50)  # Avoid warinings when saving
        model_wrap.disc_model.save(config.save_path + '_disc_weights.hdf5')
        model_wrap.gen_model.save(config.save_path + '_gen_weights.hdf5')
        # logging.set_verbosity(30)

    try:
        for epoch in range(config.epoch, config.num_epochs):
            tensorboard.on_epoch_begin(epoch)

            if config.lr_decay:
                learning_rate = config.learning_rate * (0.1 ** (epoch // (config.num_epochs // 3)))
                # learning_rate = config.learning_rate * (1.0 - (epoch / config.num_epochs))
                model_wrap.update_lr(learning_rate)

            t = trange(config.batch, train_batches)
            t.set_description('| ep: %d | lr: %.2e |' % (epoch, learning_rate))
            disc_loss_sum = 0.0
            gen_loss_sum = 0.0
            keep_prob = 0.8 - (0.6 * epoch / config.num_epochs)
            for batch in t:
                tensorboard.on_batch_begin(batch)

                disc_batches = 5
                # disc_batches = 55 if ((epoch < 1 and batch < train_batches // 10)
                #                           or (batch % 10 == 0)) else 5
                disc_loss = 0.0
                loss_real = 0.0
                loss_fake = 0.0
                for disc_batch in range(disc_batches):
                    labs_batch, poses_batch = train_generator.next()

                    mask_batch = poses_batch[..., 3, np.newaxis]
                    mask_batch = mask_batch * gen_mask(np.random.randint(5), keep_prob)
                    poses_batch = poses_batch[..., :3]

                    disc_inputs = [poses_batch]
                    gen_inputs = [poses_batch, mask_batch]
                    place_holders = []
                    if config.action_cond:
                        place_holders.append(labs_batch[:, 2])
                    if config.latent_cond_dim > 0:
                        gen_inputs.append(gen_latent_noise())

                    losses = model_wrap.disc_train(disc_inputs + gen_inputs + place_holders)

                    if disc_batch == 0:
                        disc_losses = losses
                    else:
                        for key in disc_losses.keys():
                            disc_losses[key] += losses[key]

                for key in disc_losses.keys():
                    disc_losses[key] /= disc_batches

                labs_batch, poses_batch = train_generator.next()

                mask_batch = poses_batch[..., 3, np.newaxis]
                mask_batch = mask_batch * gen_mask(np.random.randint(5), keep_prob)
                poses_batch = poses_batch[..., :3]

                gen_inputs = [poses_batch, mask_batch]
                place_holders = []
                if config.action_cond:
                    place_holders.append(labs_batch[:, 2])
                if config.latent_cond_dim > 0:
                    gen_inputs.append(gen_latent_noise())

                gen_losses = model_wrap.gen_train(gen_inputs + place_holders)

                # Output to terminal, note output is averaged over the epoch
                disc_loss_sum += disc_losses['train/disc_loss_wgan']
                gen_loss_sum += gen_losses['train/gen_loss_wgan']
                t.set_postfix(disc_loss='%.2e' % (disc_loss_sum / (batch + 1)),
                              gen_loss='%.2e' % (gen_loss_sum / (batch + 1)))

                logs = disc_losses.copy()
                logs.update(gen_losses)

                tensorboard.on_batch_end(batch, logs)

                config.batch = batch + 1

            labs_batch, poses_batch = val_generator.next()

            mask_batch = poses_batch[..., 3, np.newaxis]
            mask_mode = np.random.randint(5)
            mask_batch = mask_batch * gen_mask(mask_mode, keep_prob)
            poses_batch = poses_batch[..., :3]

            disc_inputs = [poses_batch]
            gen_inputs = [poses_batch, mask_batch]
            place_holders = []
            if config.action_cond:
                place_holders.append(labs_batch[:, 2])
            if config.latent_cond_dim > 0:
                gen_inputs.append(gen_latent_noise())

            disc_losses = model_wrap.disc_eval(disc_inputs + gen_inputs + place_holders)
            gen_losses = model_wrap.gen_eval(gen_inputs + place_holders)
            if config.use_pose_fae:
                fae_z = gen_losses.pop('fae_z', None)
            gen_outputs = gen_losses.pop('gen_outputs', None)

            logs = disc_losses.copy()
            logs.update(gen_losses)

            # Generating images
            if (epoch % (config.num_epochs // 10)) == 0 or epoch == (config.num_epochs - 1):
                if config.normalize_data:
                    poses_batch = data_input.denormalize_poses(poses_batch)
                    gen_outputs = data_input.denormalize_poses(gen_outputs)
                for i in range(16):  # config.batch_size
                    gif_name = '%s_tmp.gif' % config.save_path
                    gif_height, gif_width = plot_seq_gif(
                        np.concatenate([poses_batch[np.newaxis, i, ...],
                                       gen_outputs[np.newaxis, i, ...]]),
                        labs_batch[i, ...],
                        config.data_set,
                        seq_masks=mask_batch[i, ...],
                        extra_text='mask mode: %s' % mask_modes[mask_mode],
                        save_path=gif_name)

                    with open(gif_name, 'rb') as f:
                        encoded_image_string = f.read()

                    logs['custom_img_%d' % i] = {'height': gif_height,
                                                 'width': gif_width,
                                                 'enc_string': encoded_image_string}

                    if config.use_pose_fae:
                        png_name = '%s_mask_tmp.png' % config.save_path
                        plot_seq_emb(fae_z[i, ...], png_name)

                        with open(png_name, 'rb') as f:
                            encoded_image_string = f.read()

                        logs['custom_img_emb_%d' % i] = {'height': int(fae_z.shape[1]),
                                                         'width': int(fae_z.shape[2]),
                                                         'enc_string': encoded_image_string}

            tensorboard.on_epoch_end(epoch, logs)

            config.epoch = epoch + 1
            config.batch = 0

            save_models()
            config.save()

    except KeyboardInterrupt:
        save_models()
        config.save()

    tensorboard.on_train_end(None)
