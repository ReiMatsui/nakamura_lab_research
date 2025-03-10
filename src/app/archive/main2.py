import cv2
import mediapipe as mp
import numpy as np
import time
import os
import threading
import queue
from loguru import logger
from src.utils.sound_generator import SoundGenerator
from src.utils.garageband_handler import GarageBandHandler
import pandas as pd
from datetime import datetime
import pathlib
import matplotlib.pyplot as plt
from matplotlib import animation
from mpl_toolkits.mplot3d import Axes3D

class DualCameraHandFaceSoundTracker:
    def __init__(self, face_camera_no: int = 0, hand_camera_no: int = 1, width: int = 640, height: int = 360):
        """
        2台のカメラを使用する手のランドマーク、顔の向き追跡、音生成アプリケーションの初期化
        """
        os.environ['no_proxy'] = "*"
        
        # スレッド間通信用のキュー
        self.face_frame_queue = queue.Queue(maxsize=10)
        self.hand_frame_queue = queue.Queue(maxsize=10)
        self.face_result_queue = queue.Queue(maxsize=10)
        self.hand_result_queue = queue.Queue(maxsize=10)
        self.running = threading.Event()
        self.running.set()
        
        # MediaPipe設定
        self.mp_drawing = mp.solutions.drawing_utils
        self.mp_drawing_styles = mp.solutions.drawing_styles
        self.mp_hands = mp.solutions.hands
        self.mp_face_mesh = mp.solutions.face_mesh
        
        # カメラ設定
        self.face_capture = cv2.VideoCapture(face_camera_no)
        self.hand_capture = cv2.VideoCapture(hand_camera_no)
        
        for capture in [self.face_capture, self.hand_capture]:
            capture.set(cv2.CAP_PROP_FRAME_WIDTH, width)
            capture.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        
        # 動画保存の設定
        self.frame_width = width
        self.frame_height = height
        self.fps = 20.0
        
        # 音ジェネレーター設定
        try:
            output_names = SoundGenerator.get_output_names()
            self.sound_generator = SoundGenerator(output_name=output_names[0])
        except Exception as e:
            logger.exception("音ジェネレーターの初期化に失敗")
            raise
        
        # データ保存用の設定
        self.face_orientation_data = []
        self.hand_trajectory_data = {}
        
        # セッション設定
        self.session_start_time = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.base_output_dir = pathlib.Path("output")
        self.session_dir = self.base_output_dir / self.session_start_time
        self.session_dir.mkdir(parents=True, exist_ok=True)
        
        # 動画ライター初期化
        face_video_path = str(self.session_dir / 'face_tracking_video.mp4')
        hand_video_path = str(self.session_dir / 'hand_tracking_video.mp4')
        fourcc = cv2.VideoWriter_fourcc(*'mp4v')
        self.face_video_writer = cv2.VideoWriter(
            face_video_path, 
            fourcc, 
            self.fps, 
            (self.frame_width, self.frame_height)
        )
        self.hand_video_writer = cv2.VideoWriter(
            hand_video_path, 
            fourcc, 
            self.fps, 
            (self.frame_width, 480)
        )
        
        # 処理スレッド初期化
        self.face_process_thread = threading.Thread(target=self._process_face_frames)
        self.hand_process_thread = threading.Thread(target=self._process_hand_frames)
        self.face_process_thread.daemon = True
        self.hand_process_thread.daemon = True
        
        logger.info(f"セッションディレクトリを作成しました: {self.session_dir}")

    def _process_face_frames(self):
        """
        別スレッドで顔のMediaPipe処理を実行
        """
        try:
            with self.mp_face_mesh.FaceMesh(
                static_image_mode=False,
                max_num_faces=1,
                min_detection_confidence=0.5,
                min_tracking_confidence=0.5
            ) as face_mesh:
                while self.running.is_set():
                    try:
                        frame = self.face_frame_queue.get(timeout=1.0)
                        if frame is None:
                            continue
                        
                        frame_copy = frame.copy()
                        image_rgb = cv2.cvtColor(frame_copy, cv2.COLOR_BGR2RGB)
                        face_results = face_mesh.process(image_rgb)
                        
                        results = {
                            'multi_face_landmarks': [
                                landmark.copy() if hasattr(landmark, 'copy') 
                                else landmark 
                                for landmark in (face_results.multi_face_landmarks or [])
                            ] if face_results.multi_face_landmarks else None
                        }
                        
                        self.face_result_queue.put((results, frame_copy))
                        
                    except queue.Empty:
                        continue
                    except Exception as e:
                        logger.exception("顔フレーム処理中にエラーが発生")
                        continue
                        
        except Exception as e:
            logger.exception("顔MediaPipe処理スレッドでエラーが発生")
        finally:
            logger.info("顔MediaPipe処理スレッドを終了します")

    def _process_hand_frames(self):
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
                        logger.exception("手フレーム処理中にエラーが発生")
                        continue
                        
        except Exception as e:
            logger.exception("手MediaPipe処理スレッドでエラーが発生")
        finally:
            logger.info("手MediaPipe処理スレッドを終了します")

    def run(self):
        """
        メインアプリケーションループ
        """
        cv2.startWindowThread()
        try:
            # 処理スレッドを開始
            self.face_process_thread.start()
            self.hand_process_thread.start()
            
            while (self.face_capture.isOpened() and 
                   self.hand_capture.isOpened() and 
                   self.running.is_set()):
                
                # 顔カメラからフレームを取得
                face_ret, face_frame = self.face_capture.read()
                if not face_ret:
                    break
                face_frame = cv2.flip(face_frame, 1)
                
                # 手カメラからフレームを取得
                hand_ret, hand_frame = self.hand_capture.read()
                if not hand_ret:
                    break
                hand_frame = cv2.flip(hand_frame, 1)
                
                # フレームを処理キューに追加
                try:
                    self.face_frame_queue.put(face_frame.copy(), timeout=0.1)
                    self.hand_frame_queue.put(hand_frame.copy(), timeout=0.1)
                except queue.Full:
                    continue
                
                # 処理結果を取得
                try:
                    face_results, processed_face_frame = self.face_result_queue.get(timeout=0.1)
                    hand_results, processed_hand_frame = self.hand_result_queue.get(timeout=0.1)
                except queue.Empty:
                    continue
                
                face_image = processed_face_frame.copy()
                hand_image = processed_hand_frame.copy()

                # 手のランドマーク処理
                if hand_results['multi_hand_landmarks']:
                    for i, landmarks in enumerate(hand_results['multi_hand_landmarks']):
                        try:
                            self.mp_drawing.draw_landmarks(
                                hand_image,
                                landmarks,
                                self.mp_hands.HAND_CONNECTIONS,
                                self.mp_drawing_styles.get_default_hand_landmarks_style(),
                                self.mp_drawing_styles.get_default_hand_connections_style()
                            )
                            
                            self._process_hand_data(landmarks, i)
                            
                            if i == 0:
                                hand_x = landmarks.landmark[9].x
                                hand_y = landmarks.landmark[9].y
                                handedness = hand_results['handedness'][0].classification[0].label
                                
                                self.sound_generator.update_hand_orientation(landmarks, handedness)
                                
                                new_notes = self.sound_generator.new_notes(hand_x, hand_y)
                                self.sound_generator.update_notes(new_notes)
                            
                            is_palm_up = self.sound_generator.is_palm_up
                            cv2.putText(hand_image, f'Palm up: {is_palm_up}', 
                                      (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 255, 0), 2)
                
                        except Exception as e:
                            logger.error(f"ハンドランドマーク処理中のエラー: {e}")
                            continue
                
                # 顔の向き処理
                if face_results['multi_face_landmarks']:
                    try:
                        face_landmarks = face_results['multi_face_landmarks'][0]
                        yaw, pitch, roll = self._calculate_face_orientation(face_landmarks)
                        self.face_orientation_data.append([time.time(), yaw, pitch, roll])
                        
                    except Exception as e:
                        logger.error(f"顔の向き処理中のエラー: {e}")
                
                try:
                    self.hand_video_writer.write(hand_image)
                    self.face_video_writer.write(face_image)
                    cv2.imshow('Face Tracking', face_image)
                    cv2.imshow('Hand Tracking', hand_image)
                except Exception as e:
                    logger.error(f"画像表示/保存中のエラー: {e}")
                
                if cv2.waitKey(3) == ord('q'):
                    break
        
        except Exception as e:
            logger.exception("メインループでエラーが発生")
        
        finally:
            # クリーンアップ
            self.running.clear()
            
            # キューをクリア
            for q in [self.face_frame_queue, self.hand_frame_queue, 
                     self.face_result_queue, self.hand_result_queue]:
                while not q.empty():
                    try:
                        q.get_nowait()
                    except queue.Empty:
                        break
            
            self.face_process_thread.join(timeout=2.0)
            self.hand_process_thread.join(timeout=2.0)
            self._save_data()
            self._create_face_orientation_plots()
            self._create_3d_trajectory_animation()
            self.sound_generator.end()
            
            # OpenCVリソースの解放
            if self.face_video_writer is not None:
                self.face_video_writer.release()
            if self.hand_video_writer is not None:
                self.hand_video_writer.release()
            cv2.waitKey(1)
            if self.face_capture is not None:
                self.face_capture.release()
            if self.hand_capture is not None:
                self.hand_capture.release()
            cv2.waitKey(1)
            cv2.destroyAllWindows()
            
            logger.info("アプリケーションを終了しました")
            
    def _calculate_face_orientation(self, landmarks):
            """
            顔の向き（yaw, pitch, roll）を計算
            
            Returns:
                tuple: (yaw, pitch, roll) in degrees
                - yaw: 左右の回転角度 (-: 左, +: 右)
                - pitch: 上下の回転角度 (-: 上, +: 下)
                - roll: 首の傾き (-: 左傾き, +: 右傾き)
            """
            try:
                # 必要なランドマークのインデックス
                # 鼻先
                nose_tip = landmarks.landmark[4]
                # 両目の外側と内側のポイント
                left_eye_outer = landmarks.landmark[33]
                left_eye_inner = landmarks.landmark[133]
                right_eye_inner = landmarks.landmark[362]
                right_eye_outer = landmarks.landmark[263]
                
                # Yawの計算 (左右の回転)
                eye_center_x = (left_eye_outer.x + right_eye_outer.x) / 2
                eye_distance = abs(left_eye_outer.x - right_eye_outer.x)
                yaw = np.arctan2(nose_tip.x - eye_center_x, eye_distance) * 180 / np.pi
                
                # Pitchの計算 (上下の回転)
                eye_center_y = (left_eye_outer.y + right_eye_outer.y) / 2
                pitch = np.arctan2(nose_tip.y - eye_center_y, eye_distance) * 180 / np.pi
                
                # Rollの計算 (首の傾き)
                # 両目の傾きから計算
                dy = right_eye_outer.y - left_eye_outer.y
                dx = right_eye_outer.x - left_eye_outer.x
                roll = np.arctan2(dy, dx) * 180 / np.pi
                
                return yaw, pitch, roll
                
            except Exception as e:
                logger.error(f"顔の向き計算中にエラー: {e}")
                return 0, 0, 0

    def _process_hand_data(self, landmarks, hand_id):
        """
        手のランドマークデータを処理
        """
        try:
            landmark_9 = landmarks.landmark[9]
            timestamp = time.time()
            
            if hand_id not in self.hand_trajectory_data:
                self.hand_trajectory_data[hand_id] = {
                    'timestamp': [],
                    'x': [],
                    'y': [],
                    'z': []
                }
            
            self.hand_trajectory_data[hand_id]['timestamp'].append(timestamp)
            self.hand_trajectory_data[hand_id]['x'].append(landmark_9.x)
            self.hand_trajectory_data[hand_id]['y'].append(landmark_9.y)
            self.hand_trajectory_data[hand_id]['z'].append(landmark_9.z)
        except Exception as e:
            logger.error(f"手のデータ処理中にエラー: {e}")

    def _create_face_orientation_plots(self):
        """
        顔の向きのグラフを作成して保存
        """
        if not self.face_orientation_data:
            logger.warning("顔の向きデータがありません")
            return
            
        try:
            fig, (ax1, ax2, ax3) = plt.subplots(3, 1, figsize=(12, 12))
            
            df = pd.DataFrame(self.face_orientation_data, 
                                columns=['timestamp', 'yaw', 'pitch', 'roll'])
            df['relative_time'] = df['timestamp'] - df['timestamp'].iloc[0]
            
            ax1.plot(df['relative_time'], df['yaw'], 'b-', linewidth=1)
            ax1.set_title('Yaw (Left/Right) Over Time')
            ax1.set_ylabel('Angle (degrees)')
            ax1.grid(True)
            
            ax2.plot(df['relative_time'], df['pitch'], 'r-', linewidth=1)
            ax2.set_title('Pitch (Up/Down) Over Time')
            ax2.set_ylabel('Angle (degrees)')
            ax2.grid(True)
            
            ax3.plot(df['relative_time'], df['roll'], 'g-', linewidth=1)
            ax3.set_title('Roll (Head Tilt) Over Time')
            ax3.set_xlabel('Time (seconds)')
            ax3.set_ylabel('Angle (degrees)')
            ax3.grid(True)
            
            plt.tight_layout()
            
            plot_path = self.session_dir / 'face_orientation_plot.png'
            plt.savefig(plot_path, dpi=300, bbox_inches='tight')
            plt.close(fig)
            
            logger.info(f"顔の向きグラフを保存しました: {plot_path}")
        except Exception as e:
            logger.error(f"グラフ作成中にエラー: {e}")

    def _create_3d_trajectory_animation(self):
        """
        手の軌跡の3Dアニメーションを作成して保存
        """
        if not self.hand_trajectory_data:
            logger.warning("手の軌跡データがありません")
            return

        try:
            # データを単純な配列に変換
            timestamps = []
            x_coords = []
            y_coords = []
            z_coords = []
            
            for data in self.hand_trajectory_data.values():
                timestamps.extend(data['timestamp'])
                x_coords.extend(data['x'])
                y_coords.extend(data['y'])
                z_coords.extend(data['z'])

            # データを時系列でソート
            sorted_indices = np.argsort(timestamps)
            x_coords = np.array(x_coords)[sorted_indices]
            y_coords = np.array(y_coords)[sorted_indices]
            z_coords = np.array(z_coords)[sorted_indices]
            timestamps = np.array(timestamps)[sorted_indices]

            # 3Dプロットの設定
            fig = plt.figure(figsize=(12, 8))
            ax = fig.add_subplot(111, projection='3d')

            # プロット用のラインとポイントを作成
            line = ax.plot([], [], [], 
                        c='blue',
                        alpha=0.5,
                        linewidth=2)[0]
            point = ax.plot([], [], [],
                        'o',
                        c='red',
                        markersize=8)[0]

            margin = 0.1
            ax.set_xlim([min(x_coords) - margin, max(x_coords) + margin])
            ax.set_ylim([min(y_coords) - margin, max(y_coords) + margin])
            ax.set_zlim([min(z_coords) - margin, max(z_coords) + margin])
            
            ax.set_xlabel('X')
            ax.set_ylabel('Y')
            ax.set_zlabel('Z')
            ax.set_title('Hand Trajectory (3D)')
            ax.grid(True)
            
            trail_length = 30

            def update(frame):
                ax.view_init(elev=20, azim=frame)
                
                start_idx = max(0, frame - trail_length)
                end_idx = frame + 1
                
                if end_idx > len(x_coords):
                    end_idx = len(x_coords)
                    start_idx = max(0, end_idx - trail_length)
                
                # 軌跡の更新
                line.set_data(x_coords[start_idx:end_idx],
                            y_coords[start_idx:end_idx])
                line.set_3d_properties(z_coords[start_idx:end_idx])
                
                # 現在位置の点の更新
                if end_idx > 0:
                    point.set_data([x_coords[end_idx-1]], 
                                [y_coords[end_idx-1]])
                    point.set_3d_properties([z_coords[end_idx-1]])
                
                return line, point

            # アニメーションの作成と保存
            num_frames = len(timestamps)
            ani = animation.FuncAnimation(fig, 
                                        update,
                                        frames=num_frames,
                                        interval=50,
                                        blit=True)

            animation_path = self.session_dir / 'hand_trajectory_3d.mp4'
            writer = animation.FFMpegWriter(fps=20,
                                        metadata=dict(artist='HandTracker'),
                                        bitrate=5000)
            ani.save(str(animation_path), writer=writer)
            
            plt.close(fig)
            logger.info(f"3Dアニメーションを保存しました: {animation_path}")
        except Exception as e:
            logger.error(f"3Dアニメーション作成中にエラー: {e}")

    def _save_data(self):
        """
        すべてのデータをCSVファイルに保存
        """
        try:
            # 顔の向きデータの保存
            if self.face_orientation_data:
                df_face = pd.DataFrame(self.face_orientation_data,
                                    columns=['timestamp', 'yaw', 'pitch', 'roll'])
                df_face['relative_time'] = df_face['timestamp'] - df_face['timestamp'].iloc[0]
                df_face.to_csv(self.session_dir / 'face_orientation.csv', index=False)
                
            # 手の軌跡データの保存
            if self.hand_trajectory_data:
                dfs = []
                for hand_id, data in self.hand_trajectory_data.items():
                    df = pd.DataFrame(data)
                    df['hand_id'] = hand_id
                    dfs.append(df)
                
                df_hands = pd.concat(dfs, ignore_index=True)
                df_hands['relative_time'] = df_hands['timestamp'] - df_hands['timestamp'].min()
                df_hands.to_csv(self.session_dir / 'hand_trajectories.csv', index=False)
            
            logger.info("すべてのデータを保存しました")
        except Exception as e:
            logger.error(f"データ保存中にエラー: {e}")
            
            
def main():
    """
    アプリケーション起動
    """
    try:
        # ログの設定
        logger.add(
            "logs/app_{time}.log",
            rotation="1 day",
            retention="7 days",
            level="INFO",
            encoding="utf-8"
        )
        logger.info("アプリケーションを開始します")
        
        # トラッカーの初期化と実行
        tracker = DualCameraHandFaceSoundTracker()
        tracker.run()
        
    except Exception as e:
        logger.exception("アプリケーションの起動に失敗")
    
    finally:
        # 最終的なクリーンアップ
        cv2.destroyAllWindows()
        cv2.waitKey(1)  # ウィンドウを確実に閉じる

if __name__ == '__main__':
    main()