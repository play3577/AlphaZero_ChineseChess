from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from logging import getLogger
from threading import Lock, Condition

# import chess
import numpy as np

from chess_zero.config import Config, INIT_STATE
import chess_zero.env.chess_env as env
from time import sleep

logger = getLogger(__name__)


# these are from AGZ nature paper
class VisitStats:
    def __init__(self):
        self.a = defaultdict(ActionStats)
        self.sum_n = 0
        self.visit = []
        self.p = None
        self.legal_moves = None
        self.waiting = False


class ActionStats:
    def __init__(self):
        self.n = 0
        self.w = 0
        self.q = 0
        self.p = -1
        self.next = None


class ChessPlayer:
    # dot = False
    def __init__(self, config: Config, search_tree=None, pipe=None, play_config=None, dummy=False):
        self.moves = []

        self.config = config
        self.play_config = play_config or self.config.play
        self.labels_n = config.n_labels
        self.labels = config.labels
        # self.move_lookup = {Move.from_uci(move): i for move, i in zip(self.labels, range(self.labels_n))}
        self.move_lookup = {move: i for move, i in zip(self.labels, range(self.labels_n))}
        if dummy:
            return

        self.pipe = pipe

        self.node_lock = defaultdict(Lock)

        self.s_lock = Lock()
        self.run_lock = Lock()
        self.q_lock = Lock()
        self.t_lock = Lock()
        self.buffer_planes = []
        self.buffer_history = []

        self.all_done = Lock()
        self.num_task = 0

        self.job_done = False

        self.executor = ThreadPoolExecutor(max_workers=self.play_config.search_threads+2)
        self.executor.submit(self.receiver)
        self.executor.submit(self.sender)


        if search_tree is None:
            self.tree = defaultdict(VisitStats)
        else:
            self.tree = search_tree

    def close(self):
        self.job_done = True
        if self.executor is not None:
            self.executor.shutdown()

    def action(self, state:str, n_step:int) -> (str, list):
        self.all_done.acquire(True)

        self.num_task = self.play_config.simulation_num_per_move
        for i in range(self.play_config.simulation_num_per_move):
            self.executor.submit(self.search_my_move, state, [state])

        self.all_done.acquire(True)
        self.all_done.release()

        policy = self.calc_policy(state)
        my_action = int(np.random.choice(range(self.labels_n), p=self.apply_temperature(policy, n_step)))

        return self.config.labels[my_action], list(policy)


    def search_my_move(self, state:str, history=[]):  #dfs to the leaf and back up

        while True:
            v = env.game_over(state)
            if v != 0:
                self.executor.submit(self.update_tree, None, v, history)
                break

            with self.node_lock[state]:
                if state not in self.tree:
                    self.tree[state].waiting = True
                    self.tree[state].legal_moves = env.legal_moves(state)
                    self.expand_and_evaluate(state, history)
                    break

                if state in history[:-1]: # loop -> loss
                    self.executor.submit(self.update_tree, None, 0, history)
                    break

                my_visit_stats = self.tree[state]
                if my_visit_stats.waiting:
                    my_visit_stats.visit.append(history)
                    break

                # SELECT STEP
                canon_action = self.select_action_q_and_u(state, state==INIT_STATE)


                my_visit_stats = self.tree[state]
                my_visit_stats.sum_n += 1

                my_stats = my_visit_stats.a[canon_action]
                my_stats.n += 1
                my_stats.w += 0
                my_stats.q = my_stats.w / my_stats.n

                if my_stats.next is None:
                    my_stats.next = env.step(state, canon_action)

            history.append(canon_action)
            state = my_stats.next
            history.append(state)



    def sender(self):
        limit = 256
        while not self.job_done:
            self.run_lock.acquire()
            # print('acquired')
            with self.q_lock:
                l = min(limit, len(self.buffer_history))
                if l > 0:
                    t_data = self.buffer_planes[0:l]
                    # print('send %d' % (l))
                    self.pipe.send(t_data)
                else:
                    # print('released  wait %d' % (self.num_task))
                    self.run_lock.release()
                    sleep(0.001)

    def receiver(self):
        while not self.job_done:
            if self.pipe.poll(0.001):
                rets = self.pipe.recv()
            else:
                continue
            k = 0
            with self.q_lock:
                for ret in rets:
                    self.executor.submit(self.update_tree, ret[0], ret[1], self.buffer_history[k])
                    k = k+1
                # print('api recv %d' % (k))
                self.buffer_planes = self.buffer_planes[k:]
                self.buffer_history = self.buffer_history[k:]
            # print('released')
            self.run_lock.release()

    def update_tree(self, p, v, history):
        state = history.pop()
        if p is not None:
            with self.node_lock[state]:
                self.tree[state].p = p
                self.tree[state].waiting = False
                for hist in self.tree[state].visit:
                    self.executor.submit(self.search_my_move, state, hist)
                self.tree[state].visit = []

        while len(history) > 0:
            action = history.pop()
            state = history.pop()
            v = -v
            with self.node_lock[state]:
                my_visit_stats = self.tree[state]
                my_stats = my_visit_stats.a[action]
                my_stats.w += v
                my_stats.q = my_stats.w / my_stats.n


        with self.t_lock:
            self.num_task -= 1
            if (self.num_task <= 0):
                self.all_done.release()




    def expand_and_evaluate(self, state:str, history:str):
        """ expand new leaf, this is called only once per state
        this is called with state locked
        insert P(a|s), return leaf_v
        """
        state_planes = env.canon_input_planes(state)
        with self.q_lock:
            self.buffer_planes.append(state_planes)
            self.buffer_history.append(history)


    #@profile
    def select_action_q_and_u(self, state, is_root_node) -> str:

        my_visitstats = self.tree[state]
        legal_moves = my_visitstats.legal_moves



        if my_visitstats.p is not None: #push p, the prior probability to the edge (my_visitstats.p)


            tot_p = 0
            for mov in legal_moves:
                mov_p = my_visitstats.p[self.move_lookup[mov]]
                my_visitstats.a[mov].p = mov_p
                tot_p += mov_p


            for mov in legal_moves:
                my_visitstats.a[mov].p /= tot_p
            my_visitstats.p = None # release the temp policy

        xx_ = np.sqrt(my_visitstats.sum_n + 1)  # sqrt of sum(N(s, b); for all b)

        e = self.play_config.noise_eps
        c_puct = self.play_config.c_puct
        dir_alpha = self.play_config.dirichlet_alpha

        best_s = -999
        best_a = None

        for action in legal_moves:
            a_s = my_visitstats.a[action]
            p_ = a_s.p
            if is_root_node:
                p_ = (1-e) * p_ + e * np.random.dirichlet([dir_alpha])
            b = a_s.q + c_puct * p_ * xx_ / (1 + a_s.n)
            if b > best_s:
                best_s = b
                best_a = action

        return best_a

    def apply_temperature(self, policy, turn):
        tau = np.power(self.play_config.tau_decay_rate, turn + 1)
        if tau < 0.1:
            tau = 0
        if tau == 0:
            action = np.argmax(policy)
            ret = np.zeros(self.labels_n)
            ret[action] = 1.0
            return ret
        else:
            ret = np.power(policy, 1/tau)
            ret /= np.sum(ret)
            return ret

    def calc_policy(self, state):
        """calc π(a|s0)
        :return:
        """
        my_visitstats = self.tree[state]
        policy = np.zeros(self.labels_n)
        for action, a_s in my_visitstats.a.items():
            policy[self.move_lookup[action]] = a_s.n

        policy /= np.sum(policy)

        return policy

