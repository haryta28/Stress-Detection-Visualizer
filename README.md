# Stress-Detection-Visualizer
Real-time emotional state detection using ESP32 + HRV/GSR sensors, classified via Python and visualised as a coloured body silhouette in TouchDesigner.
 Real-time stress detection and biofeedback visualisation using biosensors and generative media.

## What it does
Places a finger on the sensor pad → ESP32 reads your pulse and sweat response → 
Python classifies your emotional state (Zen / Stress / Flow / Burnout) → 
TouchDesigner projects your silhouette in a matching colour onto a wall in real time.

## Stack
- **Hardware:** ESP32, MAX30102 pulse sensor, GSR sensor
- **Firmware:** C++ (Arduino IDE)
- **Middleware:** Python (`osc.py`) — serial parsing, baseline calibration, OSC output
- **Visuals:** TouchDesigner — difference matte silhouette, Switch TOP colour routing

## Emotional States
|  State  |     Condition      |   Colour |
|---------|--------------------|----------|
|  Zen    |  High HRV, Low GSR | Violet   |
|  Stress |  Low HRV, High GSR | Hot Pink |
| Flow    | High HRV, High GSR |    Cyan  |
| Burnout |  Low HRV, Low GSR  |    Teal  |

