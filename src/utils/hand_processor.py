import cv2
import mediapipe as mp
import threading
import queue
import numpy as np
from loguru import logger
from math import sqrt
from src.utils.sound_generator import SoundGenerator
from src.utils.data_recorder import DataRecorder
from src.config import setting

class HandProcessor:
    """ 
    mediapipeで手の処理を行うクラス
    """
    def __init__(self, data_recorder: DataRecorder):
        # 音ジェネレーター設定
        try:
            output_names = SoundGenerator.get_output_names()
            port = setting.midi_output_port
            self.sound_generator = SoundGenerator(output_name=output_names[port])
        except Exception as e:
            logger.exception(f"音ジェネレーターの初期化に失敗:{e}")
            raise
        
        self.data_recorder = data_recorder
        self.mp_hands = mp.solutions.hands
        self.hands = self.mp_hands.Hands(
            static_image_mode=False,
            max_num_hands=2,
            min_detection_confidence=0.5
        )
        self.mp_drawing = mp.solutions.drawing_utils
        self.mp_drawing_styles = mp.solutions.drawing_styles
        
        self.hand_frame_queue = queue.Queue(maxsize=10)
        self.hand_result_queue = queue.Queue(maxsize=10)
        self.running = threading.Event()
        self.process_thread = threading.Thread(target=self.process_frame)
        self.process_thread.daemon = True
        self.running.set()
    
    def start(self):
        self.process_thread.start()
        return
    
    def clean_up(self):
        """
        別スレッドでのmediapipe処理を終了し、キューをクリア
        """
        self.running.clear()
        # キューをクリア
        for q in [self.hand_frame_queue, self.hand_result_queue]:
            while not q.empty():
                try:
                    q.get_nowait()
                except queue.Empty:
                    break
        self.process_thread.join(timeout=2.0)
        self.sound_generator.end()
                
    def put_to_queue(self, frame):
        self.hand_frame_queue.put(frame, timeout=0.1)

    def get_from_queue(self):
        hand_results, processed_hand_frame  = self.hand_result_queue.get(timeout=0.1)
        return hand_results, processed_hand_frame 
    
    def judge_palm_up(self, landmarks, handedness) -> bool:
        """
        手のひらの向きを計算
        
        手のひらの法線ベクトルを計算し、上向きかどうかを判定
        landmark[0]: 手首
        landmark[5, 9, 13, 17]: 指の付け根
        """
        try:
            if_left = (handedness == "Left")
            # 人差し指の付け根
            point5 = np.array([landmarks.landmark[5].x,
                                  landmarks.landmark[5].y])
            # 小指の付け根
            point17 = np.array([landmarks.landmark[17].x,
                                  landmarks.landmark[17].y])          
            
            is_palm_up = (landmarks.landmark[8].x < landmarks.landmark[20].x) if if_left else (landmarks.landmark[8].x > landmarks.landmark[20].x )
            return is_palm_up
            
        except Exception as e:
            logger.error(f"手のひらの向き計算中にエラー: {e}")
            return False
        
    def process_frame(self):
        """
        別スレッドで手のMediaPipe処理を実行
        """
        try:
            with self.mp_hands.Hands(
                static_image_mode=False,
                max_num_hands=2,
                min_detection_confidence=0.5
            ) as hands:
                while self.running.is_set():
                    try:
                        frame = self.hand_frame_queue.get(timeout=1.0)
                        if frame is None:
                            continue
                        
                        frame_copy = frame.copy()
                        image_rgb = cv2.cvtColor(frame_copy, cv2.COLOR_BGR2RGB)
                        hands_results = hands.process(image_rgb)
                        
                        results = {
                            'multi_hand_landmarks': [
                                landmark.copy() if hasattr(landmark, 'copy') 
                                else landmark 
                                for landmark in (hands_results.multi_hand_landmarks or [])
                            ],
                            'handedness': hands_results.multi_handedness
                        }
                        
                        self.hand_result_queue.put((results, frame_copy))
                        
                    except queue.Empty:
                        continue
                    except Exception as e:
                        logger.exception(f"{e}:手フレーム処理中にエラーが発生")
                        continue
                        
        except Exception as e:
            logger.exception(f"{e}:手のMediaPipe処理スレッドでエラーが発生")
        finally:
            logger.info("手のMediaPipe処理スレッドを終了します")

    def draw_landmarks(self, image, landmarks):
        self.mp_drawing.draw_landmarks(
            image,
            landmarks,
            self.mp_hands.HAND_CONNECTIONS,
            self.mp_drawing_styles.get_default_hand_landmarks_style(),
            self.mp_drawing_styles.get_default_hand_connections_style()
        )
        
    def process_hand_landmarks(self, image, hand_results):
        """
        手のランドマークの処理と描画を行う
        """
        try:
            for i, landmarks in enumerate(hand_results['multi_hand_landmarks']):
                # ランドマークの描画
                self.draw_landmarks(image, landmarks)
                
                # サウンドジェネレーターの更新（最初の手のみ）
                if i == 0 and self.sound_generator is not None:
                    hand_x = landmarks.landmark[9].x
                    hand_y = landmarks.landmark[9].y

                    hand_z = sqrt((landmarks.landmark[9].x - landmarks.landmark[0].x)**2 + (landmarks.landmark[9].y -landmarks.landmark[0].y)**2) -0.18
                    hand_z = max(0, hand_z) * 2
                    handedness = hand_results['handedness'][0].classification[0].label
                    
                    # 手のひらが上向きか判定
                    is_palm_up = self.judge_palm_up(landmarks, handedness)

                    # 手の位置データ保存
                    self.data_recorder.record_hand_trajectory(landmarks, i, is_palm_up)
  
                    new_notes = self.sound_generator.new_notes(hand_x, hand_y, hand_z, is_palm_up)
                    self.sound_generator.update_notes(new_notes)
                    
                    # Palm upの状態を表示
                    cv2.putText(image, f'Palm up: {is_palm_up}', 
                              (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 255, 0), 2)
                    # x,y,z座標を縦に表示
                    cv2.putText(image, f'X: {hand_x:.2f}', 
                                (10, 70), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 255, 0), 2)
                    cv2.putText(image, f'Y: {hand_y:.2f}', 
                                (10, 110), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 255, 0), 2)
                    cv2.putText(image, f'Z: {hand_z:.2f}', 
                                (10, 150), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 255, 0), 2)
        except Exception as e:
                logger.error(f"ハンドランドマーク処理中のエラー: {e}")
    
    def process_hand_landmarks2(self, image, hand_results, hand_results2):
        """
        手のランドマークの処理と描画を行う
        ２つのカメラで奥行きも取得
        """
        try:            
            for i, landmarks in enumerate(hand_results['multi_hand_landmarks']):
                if hand_results2['multi_hand_landmarks']:
                    hand_z = hand_results2['multi_hand_landmarks'][0].landmark[9].x
                    hand_z = max((0.7-hand_z)*2, 0)
                else:
                    hand_z =0.5
                # ランドマークの描画
                self.draw_landmarks(image, landmarks)
                landmarks.landmark[9].z = hand_z
                
                # サウンドジェネレーターの更新（最初の手のみ）
                if i == 0 and self.sound_generator is not None:
                    hand_x = landmarks.landmark[9].x
                    hand_y = landmarks.landmark[9].y

                    hand_z = hand_z
                    handedness = hand_results['handedness'][0].classification[0].label
                    
                    # 手のひらが上向きか判定
                    is_palm_up = self.judge_palm_up(landmarks, handedness)
                    # 手の位置データ保存
                    self.data_recorder.record_hand_trajectory(landmarks, i, is_palm_up)

                    new_notes = self.sound_generator.new_notes(hand_x, hand_y, hand_z, is_palm_up)
                    self.sound_generator.update_notes(new_notes)
                    
                    # Palm upの状態を表示
                    cv2.putText(image, f'Palm up: {is_palm_up}', 
                              (10, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 2)
                    # x,y,z座標を縦に表示
                    cv2.putText(image, f'X: {hand_x:.2f}', 
                                (10, 40), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 2)
                    cv2.putText(image, f'Y: {hand_y:.2f}', 
                                (10, 60), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 2)
                    cv2.putText(image, f'Z: {hand_z:.2f}', 
                                (10, 80), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 2)
                    cv2.putText(image, f'sound_on: {self.sound_generator.is_active}', 
                                (10, 100), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 2)
                    cv2.putText(image, f'sound_changeable: {self.sound_generator.is_changeable}', 
                                (10, 120), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 2)
                           
        except Exception as e:
            logger.error(f"ハンドランドマーク処理中のエラー: {e}")