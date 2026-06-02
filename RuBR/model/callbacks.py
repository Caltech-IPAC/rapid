import tensorflow as tf


class F1EarlyStopping(tf.keras.callbacks.Callback):
    def __init__(
        self,
        precision_key,
        recall_key,
        patience=10,
        restore_best_weights=True,
    ):
        super().__init__()
        self.precision_key = precision_key
        self.recall_key = recall_key
        self.patience = patience
        self.restore_best_weights = restore_best_weights
        self.best_f1 = 0
        self.best_weights = None
        self.wait = 0

    def on_epoch_end(self, epoch, logs=None):
        logs = logs or {}
        current_precision = logs.get(self.precision_key, 0)
        current_recall = logs.get(self.recall_key, 0)
        if current_precision + current_recall == 0:
            current_f1 = 0
        else:
            current_f1 = 2 * current_precision * current_recall / (current_precision + current_recall)

        if current_f1 > self.best_f1:
            self.best_f1 = current_f1
            self.wait = 0
            if self.restore_best_weights:
                self.best_weights = self.model.get_weights()
            return

        self.wait += 1
        if self.wait >= self.patience:
            print(f"\nEarly stopping triggered after {epoch + 1} epochs")
            print(f"Best F1: {self.best_f1:.4f}")
            self.model.stop_training = True
            if self.restore_best_weights and self.best_weights is not None:
                print("Restoring best weights...")
                self.model.set_weights(self.best_weights)
