class BaseMode:
    def __init__(self, config, injector, daemon, name):
        self.config = config
        self.injector = injector
        self.daemon = daemon
        self.name = name

    def tick(self):
        raise NotImplementedError

    def interval(self):
        return 0.05

    def jitter_ms(self):
        return 0

    def reset(self):
        pass
