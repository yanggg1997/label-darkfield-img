#!/usr/bin/env python
# -*- coding: utf-8 -*-
import argparse
import codecs
import distutils.spawn
import os.path
import platform
import re
import sys
import subprocess
import cv2
import torch
import PIL.Image as Image
import networks
import torchvision.transforms as transforms
from networks import DLmodel
from torch.nn import functional as F
import platform
from functools import partial
from collections import defaultdict

import numpy as np

try:
    from PyQt5.QtGui import *
    from PyQt5.QtCore import *
    from PyQt5.QtWidgets import *
except ImportError:
    # needed for py3+qt4
    # Ref:
    # http://pyqt.sourceforge.net/Docs/PyQt4/incompatible_apis.html
    # http://stackoverflow.com/questions/21217399/pyqt4-qtcore-qvariant-object-instead-of-a-string
    if sys.version_info.major >= 3:
        import sip

        sip.setapi('QVariant', 2)
    from PyQt4.QtGui import *
    from PyQt4.QtCore import *

from combobox import ComboBox
from libs.resources import *
from libs.constants import *
from libs.utils import *
from libs.settings import Settings
from libs.shape import Shape, DEFAULT_LINE_COLOR, DEFAULT_FILL_COLOR
from libs.stringBundle import StringBundle
from libs.canvas import Canvas
from libs.zoomWidget import ZoomWidget
from libs.labelDialog import LabelDialog
from libs.colorDialog import ColorDialog
from libs.labelFile import LabelFile, LabelFileError, LabelFileFormat
from libs.toolBar import ToolBar
from libs.pascal_voc_io import PascalVocReader
from libs.pascal_voc_io import XML_EXT
from libs.yolo_io import YoloReader
from libs.yolo_io import TXT_EXT
from libs.create_ml_io import CreateMLReader
from libs.create_ml_io import JSON_EXT
from libs.ustr import ustr
from libs.hashableQListWidgetItem import HashableQListWidgetItem

__appname__ = 'labelDarkFieldImg'


class WindowMixin(object):

    def menu(self, title, actions=None):
        menu = self.menuBar().addMenu(title)
        if actions:
            addActions(menu, actions)
        return menu

    def toolbar(self, title, actions=None):
        toolbar = ToolBar(title)
        toolbar.setObjectName(u'%sToolBar' % title)
        # toolbar.setOrientation(Qt.Vertical)
        toolbar.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
        if actions:
            addActions(toolbar, actions)
        self.addToolBar(Qt.LeftToolBarArea, toolbar)
        return toolbar


class MainWindow(QMainWindow, WindowMixin):  # 程序的主逻辑均在该类中
    FIT_WINDOW, FIT_WIDTH, MANUAL_ZOOM = list(range(3))  # 可能是用于控制缩放的

    def __init__(self, defaultFilename=None, defaultPrefdefClassFile=None, defaultSaveDir=None):
        super(MainWindow, self).__init__()
        self.setWindowTitle(__appname__)

        # Load setting in the main thread   加载以前的设置
        self.settings = Settings()
        self.settings.load()
        settings = self.settings

        # Load string bundle for i18n       i18n：一种编码概念，旨在不改变代码的情况下，使程序能够适应多种语言
        self.stringBundle = StringBundle.getBundle()
        getStr = lambda strId: self.stringBundle.getString(strId)

        # Save as Pascal voc xml            标注结果xml文件存储位置
        self.defaultSaveDir = defaultSaveDir
        self.labelFileFormat = settings.get(SETTING_LABEL_FILE_FORMAT,
                                            LabelFileFormat.PASCAL_VOC)  # 获取settins中的标注文件格式，默认是PASCAL_VOC

        # For loading all image under a directory
        self.mImgList = []  # 点Open Dir后，会把选中的文件夹中的文件的路径存入该list
        self.dirname = None
        self.labelHist = []  # 预定义的类别名称，后面会从文件中读取
        self.lastOpenDir = None

        # Whether we need to save or not.
        self.dirty = False  # TODO: dirty成员是什么

        self._noSelectionSlot = False  # TODO：这些又是什么
        self._beginner = True
        self.screencastViewer = self.getAvailableScreencastViewer()
        self.screencast = "https://youtu.be/p0nR2YsCY_U"

        # Load predefined classes to the list
        self.loadPredefinedClasses(defaultPrefdefClassFile)  # 加载预定义的类别

        # Main widgets and related state.
        self.labelDialog = LabelDialog(parent=self, listItem=self.labelHist)  # ☆关键部件之一：拉出一个bbox后，弹出对话框，选择类别或输入类别
        self.itemsToShapes = {}  # TODO：不知道这是啥
        self.shapesToItems = {}
        self.prevLabelText = ''

        self.auto_bbox_button = QPushButton('自动获取候选框', self)  # 自动获取候选框按钮
        self.auto_bbox_button.clicked.connect(self.autoBBoxGen)
        self.auto_bbox_button_continuous = QPushButton('自动持续获取', self)
        self.auto_bbox_button_continuous.clicked.connect(self.autoBBoxGenCont)
        self.brightness_label = QLabel('Brightness:', self)
        self.adjust_brightness_scrollBar = QScrollBar(Qt.Horizontal, self)  # 调整图像对比度的滚动条
        self.adjust_brightness_scrollBar.setMaximum(255)
        self.adjust_brightness_scrollBar.setMinimum(1)
        self.adjust_brightness_scrollBar.setValue(255)
        self.adjust_brightness_scrollBar.setPageStep(1)
        self.adjust_brightness_scrollBar.valueChanged.connect(self.adjustContrast)

        self.threshold_label = QLabel('前景亮度阈值：', self)
        self.threshold_SpinBox = QSpinBox(self)
        self.threshold_SpinBox.setValue(10)         # 阈值默认设为10
        self.threshold_SpinBox.setMinimum(0)
        self.threshold_SpinBox.setMaximum(255)
        self.area_thresh_label = QLabel('前景大小阈值(默认1000)：',self)
        self.area_thresh_SpinBox = QSpinBox(self)
        self.area_thresh_SpinBox.setMinimum(1)
        self.area_thresh_SpinBox.setMaximum(10000)
        self.area_thresh_SpinBox.setValue(1000)

        self.dip_layout = QFormLayout()
        self.dip_layout.addRow(self.auto_bbox_button)
        self.dip_layout.addRow(self.auto_bbox_button_continuous)
        self.dip_layout.addRow(self.brightness_label, self.adjust_brightness_scrollBar)
        self.dip_layout.addRow(self.threshold_label, self.threshold_SpinBox)
        self.dip_layout.addRow(self.area_thresh_label, self.area_thresh_SpinBox)

        dip_layoutContainer = QWidget()
        dip_layoutContainer.setLayout(self.dip_layout)

        self.dip_dock = QDockWidget('Image Processing', self)  # 图像显示调整，便于找到真正的gt
        self.dip_dock.setObjectName('dip')
        self.dip_dock.setWidget(dip_layoutContainer)

        # Box Labels控件组
        listLayout = QVBoxLayout()  # 主界面最右侧的那一列（没错！）
        listLayout.setContentsMargins(0, 0, 0, 0)
        # 创建一个清除已有标签的按钮
        self.cleanLabels = QPushButton('清除标注', self)
        self.cleanLabels.clicked.connect(self.cleanAllBBox)
        listLayout.addWidget(self.cleanLabels)
        # Create a widget for using default label
        self.useDefaultLabelCheckbox = QCheckBox(getStr('useDefaultLabel'))
        self.useDefaultLabelCheckbox.setChecked(False)
        self.defaultLabelTextLine = QLineEdit()
        useDefaultLabelQHBoxLayout = QHBoxLayout()
        useDefaultLabelQHBoxLayout.addWidget(self.useDefaultLabelCheckbox)
        useDefaultLabelQHBoxLayout.addWidget(self.defaultLabelTextLine)
        useDefaultLabelContainer = QWidget()  # 用于把user default label那一行的控件包装起来，好放进QDockWidget中。
        useDefaultLabelContainer.setLayout(useDefaultLabelQHBoxLayout)

        # Create a widget for edit and diffc button
        self.diffcButton = QCheckBox(getStr('useDifficult'))
        self.diffcButton.setChecked(False)
        self.diffcButton.stateChanged.connect(self.btnstate)
        self.editButton = QToolButton()
        self.editButton.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)

        # Add some of widgets to listLayout
        listLayout.addWidget(self.editButton)
        listLayout.addWidget(self.diffcButton)
        listLayout.addWidget(useDefaultLabelContainer)

        # Create and add combobox for showing unique labels in group
        self.comboBox = ComboBox(self)  # TODO：下拉单栏，用于显示unique的label？
        listLayout.addWidget(self.comboBox)

        # Create and add a widget for showing current label items
        self.labelList = QListWidget()  # 标签候选框
        self.labelList.itemActivated.connect(self.labelSelectionChanged)
        self.labelList.itemSelectionChanged.connect(self.labelSelectionChanged)
        self.labelList.itemDoubleClicked.connect(self.editLabel)
        # Connect to itemChanged to detect checkbox changes.
        self.labelList.itemChanged.connect(self.labelItemChanged)
        listLayout.addWidget(self.labelList)

        labelListContainer = QWidget()
        labelListContainer.setLayout(listLayout)  # BoxLabels中所有控件都封装起来了

        self.dock = QDockWidget(getStr('boxLabelText'), self)  # 有关Labels的整体用一个QDockWidget封装
        self.dock.setObjectName(getStr('labels'))  # 这句的用途是？
        self.dock.setWidget(labelListContainer)

        self.fileListWidget = QListWidget()
        self.fileListWidget.itemDoubleClicked.connect(self.fileitemDoubleClicked)
        filelistLayout = QVBoxLayout()
        filelistLayout.setContentsMargins(0, 0, 0, 0)
        filelistLayout.addWidget(self.fileListWidget)
        fileListContainer = QWidget()
        fileListContainer.setLayout(filelistLayout)
        self.filedock = QDockWidget(getStr('fileList'), self)
        self.filedock.setObjectName(getStr('files'))
        self.filedock.setWidget(fileListContainer)

        self.zoomWidget = ZoomWidget()
        self.colorDialog = ColorDialog(parent=self)

        self.canvas = Canvas(parent=self)  # 自定义的画布类，程序界面的主体，用于显示图像
        self.canvas.zoomRequest.connect(self.zoomRequest)  # 鼠标滚轮缩放事件与self.zoomRequest函数绑定
        self.canvas.setDrawingShapeToSquare(settings.get(SETTING_DRAW_SQUARE, False))
        self.opencvImgData = None           # 当前画布正在显示的图像

        scroll = QScrollArea()
        scroll.setWidget(self.canvas)
        scroll.setWidgetResizable(True)
        self.scrollBars = {
            Qt.Vertical: scroll.verticalScrollBar(),
            Qt.Horizontal: scroll.horizontalScrollBar()
        }
        self.scrollArea = scroll
        self.canvas.scrollRequest.connect(self.scrollRequest)

        self.canvas.newShape.connect(self.newShape)
        self.canvas.shapeMoved.connect(self.setDirty)
        self.canvas.selectionChanged.connect(self.shapeSelectionChanged)
        self.canvas.drawingPolygon.connect(self.toggleDrawingSensitive)

        self.setCentralWidget(scroll)
        self.addDockWidget(Qt.RightDockWidgetArea, self.dip_dock)
        self.addDockWidget(Qt.RightDockWidgetArea, self.dock)
        self.addDockWidget(Qt.RightDockWidgetArea, self.filedock)

        self.filedock.setFeatures(QDockWidget.DockWidgetFloatable)

        self.dockFeatures = QDockWidget.DockWidgetClosable | QDockWidget.DockWidgetFloatable
        self.dock.setFeatures(self.dock.features() ^ self.dockFeatures)
        self.dip_dock.setFeatures(self.dip_dock.features() ^ self.dockFeatures)

        # Actions
        action = partial(newAction, self)
        quit = action(getStr('quit'), self.close,
                      'Ctrl+Q', 'quit', getStr('quitApp'))

        open = action(getStr('openFile'), self.openFile,  # 左边第一个按键
                      'Ctrl+O', 'open', getStr('openFileDetail'))

        opendir = action(getStr('openDir'), self.openDirDialog,
                         'Ctrl+u', 'open', getStr('openDir'))

        copyPrevBounding = action(getStr('copyPrevBounding'), self.copyPreviousBoundingBoxes,
                                  'Ctrl+v', 'paste', getStr('copyPrevBounding'))

        changeSavedir = action(getStr('changeSaveDir'), self.changeSavedirDialog,
                               'Ctrl+r', 'open', getStr('changeSavedAnnotationDir'))

        openAnnotation = action(getStr('openAnnotation'), self.openAnnotationDialog,
                                'Ctrl+Shift+O', 'open', getStr('openAnnotationDetail'))

        openNextImg = action(getStr('nextImg'), self.openNextImg,
                             'd', 'next', getStr('nextImgDetail'))

        openPrevImg = action(getStr('prevImg'), self.openPrevImg,
                             'a', 'prev', getStr('prevImgDetail'))

        verify = action(getStr('verifyImg'), self.verifyImg,
                        'space', 'verify', getStr('verifyImgDetail'))

        save = action(getStr('save'), self.saveFile,
                      'Ctrl+S', 'save', getStr('saveDetail'), enabled=False)

        def getFormatMeta(format):
            """
            returns a tuple containing (title, icon_name) of the selected format
            """
            if format == LabelFileFormat.PASCAL_VOC:
                return ('&PascalVOC', 'format_voc')
            elif format == LabelFileFormat.YOLO:
                return ('&YOLO', 'format_yolo')
            elif format == LabelFileFormat.CREATE_ML:
                return ('&CreateML', 'format_createml')

        save_format = action(getFormatMeta(self.labelFileFormat)[0],
                             self.change_format, 'Ctrl+',
                             getFormatMeta(self.labelFileFormat)[1],
                             getStr('changeSaveFormat'), enabled=True)

        saveAs = action(getStr('saveAs'), self.saveFileAs,
                        'Ctrl+Shift+S', 'save-as', getStr('saveAsDetail'), enabled=False)

        close = action(getStr('closeCur'), self.closeFile, 'Ctrl+W', 'close', getStr('closeCurDetail'))

        deleteImg = action(getStr('deleteImg'), self.deleteImg, 'Ctrl+Shift+D', 'close', getStr('deleteImgDetail'))

        resetAll = action(getStr('resetAll'), self.resetAll, None, 'resetall', getStr('resetAllDetail'))

        color1 = action(getStr('boxLineColor'), self.chooseColor1,
                        'Ctrl+L', 'color_line', getStr('boxLineColorDetail'))

        createMode = action(getStr('crtBox'), self.setCreateMode,
                            'w', 'new', getStr('crtBoxDetail'), enabled=False)
        editMode = action('&Edit\nRectBox', self.setEditMode,
                          'Ctrl+J', 'edit', u'Move and edit Boxs', enabled=False)

        create = action(getStr('crtBox'), self.createShape,  # 对应Create RectBox选项
                        'w', 'new', getStr('crtBoxDetail'), enabled=False)
        delete = action(getStr('delBox'), self.deleteSelectedShape,  # 对应Delete RectBox选项
                        'Delete', 'delete', getStr('delBoxDetail'), enabled=False)
        copy = action(getStr('dupBox'), self.copySelectedShape,  # 对应Duplicate RectBox选项
                      'Ctrl+D', 'copy', getStr('dupBoxDetail'),
                      enabled=False)

        advancedMode = action(getStr('advancedMode'), self.toggleAdvancedMode,
                              'Ctrl+Shift+A', 'expert', getStr('advancedModeDetail'),
                              checkable=True)

        hideAll = action('&Hide\nRectBox', partial(self.togglePolygons, False),
                         'Ctrl+H', 'hide', getStr('hideAllBoxDetail'),
                         enabled=False)
        showAll = action('&Show\nRectBox', partial(self.togglePolygons, True),
                         'Ctrl+A', 'hide', getStr('showAllBoxDetail'),
                         enabled=False)

        help = action(getStr('tutorial'), self.showTutorialDialog, None, 'help', getStr('tutorialDetail'))
        showInfo = action(getStr('info'), self.showInfoDialog, None, 'help', getStr('info'))

        zoom = QWidgetAction(self)
        zoom.setDefaultWidget(self.zoomWidget)
        self.zoomWidget.setWhatsThis(
            u"Zoom in or out of the image. Also accessible with"
            " %s and %s from the canvas." % (fmtShortcut("Ctrl+[-+]"),
                                             fmtShortcut("Ctrl+Wheel")))
        self.zoomWidget.setEnabled(False)

        zoomIn = action(getStr('zoomin'), partial(self.addZoom, 10),
                        'Ctrl++', 'zoom-in', getStr('zoominDetail'), enabled=False)
        zoomOut = action(getStr('zoomout'), partial(self.addZoom, -10),
                         'Ctrl+-', 'zoom-out', getStr('zoomoutDetail'), enabled=False)
        zoomOrg = action(getStr('originalsize'), partial(self.setZoom, 100),
                         'Ctrl+=', 'zoom', getStr('originalsizeDetail'), enabled=False)
        fitWindow = action(getStr('fitWin'), self.setFitWindow,
                           'Ctrl+F', 'fit-window', getStr('fitWinDetail'),
                           checkable=True, enabled=False)
        fitWidth = action(getStr('fitWidth'), self.setFitWidth,
                          'Ctrl+Shift+F', 'fit-width', getStr('fitWidthDetail'),
                          checkable=True, enabled=False)
        # Group zoom controls into a list for easier toggling.
        zoomActions = (self.zoomWidget, zoomIn, zoomOut,
                       zoomOrg, fitWindow, fitWidth)
        self.zoomMode = self.MANUAL_ZOOM
        self.scalers = {
            self.FIT_WINDOW: self.scaleFitWindow,
            self.FIT_WIDTH: self.scaleFitWidth,
            # Set to one to scale to 100% when loading files.
            self.MANUAL_ZOOM: lambda: 1,
        }

        edit = action(getStr('editLabel'), self.editLabel,
                      'Ctrl+E', 'edit', getStr('editLabelDetail'),
                      enabled=False)
        self.editButton.setDefaultAction(edit)

        shapeLineColor = action(getStr('shapeLineColor'), self.chshapeLineColor,
                                icon='color_line', tip=getStr('shapeLineColorDetail'),
                                enabled=False)
        shapeFillColor = action(getStr('shapeFillColor'), self.chshapeFillColor,
                                icon='color', tip=getStr('shapeFillColorDetail'),
                                enabled=False)

        labels = self.dock.toggleViewAction()
        labels.setText(getStr('showHide'))
        labels.setShortcut('Ctrl+Shift+L')

        # Label list context menu.
        labelMenu = QMenu()
        addActions(labelMenu, (edit, delete))
        self.labelList.setContextMenuPolicy(Qt.CustomContextMenu)
        self.labelList.customContextMenuRequested.connect(
            self.popLabelListMenu)

        # Draw squares/rectangles
        self.drawSquaresOption = QAction('Draw Squares', self)
        self.drawSquaresOption.setShortcut('Ctrl+Shift+R')
        self.drawSquaresOption.setCheckable(True)
        self.drawSquaresOption.setChecked(settings.get(SETTING_DRAW_SQUARE, False))
        self.drawSquaresOption.triggered.connect(self.toogleDrawSquare)

        # Store actions for further handling.
        self.actions = struct(save=save, save_format=save_format, saveAs=saveAs, open=open, close=close,
                              resetAll=resetAll, deleteImg=deleteImg,
                              lineColor=color1, create=create, delete=delete, edit=edit, copy=copy,
                              createMode=createMode, editMode=editMode, advancedMode=advancedMode,
                              shapeLineColor=shapeLineColor, shapeFillColor=shapeFillColor,
                              zoom=zoom, zoomIn=zoomIn, zoomOut=zoomOut, zoomOrg=zoomOrg,
                              fitWindow=fitWindow, fitWidth=fitWidth,
                              zoomActions=zoomActions,
                              fileMenuActions=(
                                  open, opendir, save, saveAs, close, resetAll, quit),
                              beginner=(), advanced=(),
                              editMenu=(edit, copy, delete,
                                        None, color1, self.drawSquaresOption),
                              beginnerContext=(create, edit, copy, delete),
                              advancedContext=(createMode, editMode, edit, copy,
                                               delete, shapeLineColor, shapeFillColor),
                              onLoadActive=(
                                  close, create, createMode, editMode),
                              onShapesPresent=(saveAs, hideAll, showAll))

        # 工具栏
        self.menus = struct(
            file=self.menu('&File'),
            edit=self.menu('&Edit'),
            view=self.menu('&View'),
            help=self.menu('&Help'),
            recentFiles=QMenu('Open &Recent'),
            labelList=labelMenu)

        # Auto saving : Enable auto saving if pressing next
        self.autoSaving = QAction(getStr('autoSaveMode'), self)
        self.autoSaving.setCheckable(True)
        self.autoSaving.setChecked(settings.get(SETTING_AUTO_SAVE, False))
        # Sync single class mode from PR#106
        self.singleClassMode = QAction(getStr('singleClsMode'), self)
        self.singleClassMode.setShortcut("Ctrl+Shift+S")
        self.singleClassMode.setCheckable(True)
        self.singleClassMode.setChecked(settings.get(SETTING_SINGLE_CLASS, False))
        self.lastLabel = None
        # Add option to enable/disable labels being displayed at the top of bounding boxes
        self.displayLabelOption = QAction(getStr('displayLabel'), self)
        self.displayLabelOption.setShortcut("Ctrl+Shift+P")
        self.displayLabelOption.setCheckable(True)
        self.displayLabelOption.setChecked(settings.get(SETTING_PAINT_LABEL, False))
        self.displayLabelOption.triggered.connect(self.togglePaintLabelsOption)

        addActions(self.menus.file,
                   (open, opendir, copyPrevBounding, changeSavedir, openAnnotation, self.menus.recentFiles, save,
                    save_format, saveAs, close, resetAll, deleteImg, quit))
        addActions(self.menus.help, (help, showInfo))
        addActions(self.menus.view, (
            self.autoSaving,
            self.singleClassMode,
            self.displayLabelOption,
            labels, advancedMode, None,
            hideAll, showAll, None,
            zoomIn, zoomOut, zoomOrg, None,
            fitWindow, fitWidth))

        self.menus.file.aboutToShow.connect(self.updateFileMenu)

        # Custom context menu for the canvas widget:
        addActions(self.canvas.menus[0], self.actions.beginnerContext)
        addActions(self.canvas.menus[1], (
            action('&Copy here', self.copyShape),
            action('&Move here', self.moveShape)))

        self.tools = self.toolbar('Tools')
        self.actions.beginner = (
            open, opendir, changeSavedir, openNextImg, openPrevImg, verify, save, save_format, None, create, copy,
            delete, None,
            zoomIn, zoom, zoomOut, fitWindow, fitWidth)

        self.actions.advanced = (
            open, opendir, changeSavedir, openNextImg, openPrevImg, save, save_format, None,
            createMode, editMode, None,
            hideAll, showAll)

        self.statusBar().showMessage('%s started.' % __appname__)
        self.statusBar().show()

        # Application state.
        self.image = QImage()
        self.filePath = ustr(defaultFilename)
        self.lastOpenDir = None
        self.recentFiles = []
        self.maxRecent = 7
        self.lineColor = None
        self.fillColor = None
        self.zoom_level = 100
        self.fit_window = False
        # Add Chris
        self.difficult = False

        ## Fix the compatible issue for qt4 and qt5. Convert the QStringList to python list
        if settings.get(SETTING_RECENT_FILES):
            if have_qstring():
                recentFileQStringList = settings.get(SETTING_RECENT_FILES)
                self.recentFiles = [ustr(i) for i in recentFileQStringList]
            else:
                self.recentFiles = recentFileQStringList = settings.get(SETTING_RECENT_FILES)

        size = settings.get(SETTING_WIN_SIZE, QSize(600, 500))
        position = QPoint(0, 0)
        saved_position = settings.get(SETTING_WIN_POSE, position)
        # Fix the multiple monitors issue
        for i in range(QApplication.desktop().screenCount()):
            if QApplication.desktop().availableGeometry(i).contains(saved_position):
                position = saved_position
                break
        self.resize(size)
        self.move(position)
        saveDir = ustr(settings.get(SETTING_SAVE_DIR, None))
        self.lastOpenDir = ustr(settings.get(SETTING_LAST_OPEN_DIR, None))
        if self.defaultSaveDir is None and saveDir is not None and os.path.exists(saveDir):
            self.defaultSaveDir = saveDir
            self.statusBar().showMessage('%s started. Annotation will be saved to %s' %
                                         (__appname__, self.defaultSaveDir))
            self.statusBar().show()

        self.restoreState(settings.get(SETTING_WIN_STATE, QByteArray()))
        Shape.line_color = self.lineColor = QColor(settings.get(SETTING_LINE_COLOR, DEFAULT_LINE_COLOR))
        Shape.fill_color = self.fillColor = QColor(settings.get(SETTING_FILL_COLOR, DEFAULT_FILL_COLOR))
        self.canvas.setDrawingColor(self.lineColor)
        # Add chris
        Shape.difficult = self.difficult

        def xbool(x):
            if isinstance(x, QVariant):
                return x.toBool()
            return bool(x)

        if xbool(settings.get(SETTING_ADVANCE_MODE, False)):
            self.actions.advancedMode.setChecked(True)
            self.toggleAdvancedMode()

        # Populate the File menu dynamically.
        self.updateFileMenu()

        # Since loading the file may take some time, make sure it runs in the background.
        if self.filePath and os.path.isdir(self.filePath):
            self.queueEvent(partial(self.importDirImages, self.filePath or ""))
        elif self.filePath:
            self.queueEvent(partial(self.loadFile, self.filePath or ""))

        # Callbacks:
        self.zoomWidget.valueChanged.connect(self.paintCanvas)

        self.populateModeActions()

        # Display cursor coordinates at the right of status bar
        self.labelCoordinates = QLabel('')
        self.statusBar().addPermanentWidget(self.labelCoordinates)

        # Open Dir if deafult file
        if self.filePath and os.path.isdir(self.filePath):
            self.openDirDialog(dirpath=self.filePath, silent=True)

        self.device = torch.device('cuda:0' if torch.cuda.is_available() else 'cpu')
        self.model = DLmodel.model.to(self.device)
        self.model.load_state_dict(torch.load('networks/vgg11_bn_10.pt'))
        self.model.eval()
        self.transform = transforms.Compose([
            transforms.Grayscale(num_output_channels=3),
            transforms.Resize([256, 256]),
            transforms.CenterCrop([224, 224]),
            transforms.ToTensor(),
            transforms.Normalize(mean=(0.0257, 0.0257, 0.0257), std=(0.0869, 0.0869, 0.0869))
        ])
    def cleanAllBBox(self):
        self.canvas.shapes = []
        self.canvas.loadPixmap(QPixmap.fromImage(self.imageData))
        self.labelList.clear()

    def autoBBoxGenCont(self):
        if not isinstance(self.opencvImgData, np.ndarray):
            QMessageBox.critical(self,'Error','尚未选择待标注的图像，请重试！')
            return
        else:
            currIndex = self.mImgList.index(self.filePath)
            while(currIndex+1<len(self.mImgList)):
                self.openNextImg()
                currIndex = self.mImgList.index(self.filePath)
                if self.hasBoundingBoxFromAnnotationFile(self.filePath):
                    print('第{}张图像已有候选框，跳过~'.format(currIndex+1))
                    continue
                else:
                    print('正在处理第{}/{}张图像...'.format(currIndex,len(self.mImgList)))
                    self.thresholdAutodetect(self.threshold_SpinBox.value(), self.area_thresh_SpinBox.value())
                    if self.defaultSaveDir is not None:
                        if self.dirty is True:
                            self.saveFile()
                    else:
                        self.changeSavedirDialog()


    def autoBBoxGen(self):
        if not isinstance(self.opencvImgData, np.ndarray):
            QMessageBox.critical(self,'Error','尚未选择待标注的图像，请重试！')
            return
        else:
            self.thresholdAutodetect(self.threshold_SpinBox.value(),self.area_thresh_SpinBox.value())
    def genBBoxFromPoint(self,start_point, end_point,roi_img=None):
        """
        :param start_point: 要截取的bbox的左上角在真实图像上的坐标,
        :param end_point:   要截取的bbox的右下角在真实图像上的坐标
        :param roi_img:     根据ROI框来自动生成标签
        :return: 无返回值，效果就是在图上生成一个bbox
        """
        # 模拟按下create按钮
        press_point = start_point
        release_point = end_point
        self.canvas.setEditing(False)
        self.actions.create.setEnabled(False)
        # 模拟按下按键
        self.canvas.current = Shape()
        self.canvas.current.addPoint(press_point)
        self.canvas.line.points = [press_point,press_point]
        self.canvas.setHiding()
        self.actions.editMode.setEnabled(False)
        self.canvas.update()
        self.canvas.update()
        # 模拟释放按键
        if self.canvas.drawing():
            initPos = self.canvas.current[0]
            minX = initPos.x()
            minY = initPos.y()
            targetPos = release_point
            maxX = targetPos.x()
            maxY = targetPos.y()
            self.canvas.current.addPoint(QPointF(maxX,minY))
            self.canvas.current.addPoint(targetPos)
            self.canvas.current.addPoint(QPointF(minX,maxY))
         #   self.canvas.finalise()

            assert self.canvas.current
            if self.canvas.current.points[0] == self.canvas.current.points[-1]:  # 如果按下和释放鼠标为同一个位置，则不绘制bbox
                self.canvas.current = None
                self.canvas.drawingPolygon.emit(False)
                self.canvas.update()
                return
            self.canvas.current.close()
            self.canvas.shapes.append(self.canvas.current)
            self.canvas.current = None  # 绘制完一个bbox后，重新初始化current
            self.canvas.setHiding(False)

            # 根据ROI框生成标签
            print('正在预测候选框标签')
            text = self.predict3classes(roi_img)
            if text is not None:
                self.prevLabelText = text
                generate_color = generateColorByText(text)  # 给标注为不同类别的bbox分配不同的颜色
                shape = self.canvas.setLastLabel(text, generate_color, generate_color)
                self.addLabel(shape)
                if self.beginner():  # Switch to edit mode.
                    self.canvas.setEditing(True)
                    self.actions.create.setEnabled(True)
                else:
                    self.actions.editMode.setEnabled(True)
                self.setDirty()  # 看起来像是如果图像已经有过标注了，就把dirty设为true

                if text not in self.labelHist:
                    self.labelHist.append(text)
            else:
                # self.canvas.undoLastLine()
                self.canvas.resetAllLines()

        #    self.canvas.newShape.emit()  # 弹出填写标注label的对话框

            self.canvas.update()

        # 模拟标注完成，释放create按键
        self.canvas.setEditing(True)
        self.canvas.restoreCursor()
        self.actions.create.setEnabled(True)

    # ****阈值方法目标检测的核心代码****
    def thresholdAutodetect(self, threshold=10, area_thresh=1000):
        print('正在进行阈值分割获取候选框')
        img = self.opencvImgData
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        gray = cv2.blur(gray, (3, 3))
        _, bin = cv2.threshold(gray, threshold, maxval=255, type=cv2.THRESH_BINARY)
        kernel = np.ones((5, 5))
        bin = cv2.erode(bin, kernel)
        bin = cv2.dilate(bin, kernel)
        bin = cv2.dilate(bin, kernel)
        ret, labels = cv2.connectedComponents(bin)
        component = np.zeros((ret, 4))
        for i in range(ret):
            component[i][0] = labels.shape[1]
            component[i][1] = labels.shape[0]
            component[i][2] = 0
            component[i][3] = 0
        cnt = np.zeros(ret)
        for i in range(labels.shape[0]):
            for j in range(labels.shape[1]):
                label = labels[i, j]
                cnt[label] += 1

                if component[label][0] > j:
                    component[label][0] = j
                if component[label][1] > i:
                    component[label][1] = i
                if component[label][2] < j:
                    component[label][2] = j
                if component[label][3] < i:
                    component[label][3] = i
        for i in range(1, ret):
            x1 = int(component[i][0])
            y1 = int(component[i][1])
            x2 = int(component[i][2])
            y2 = int(component[i][3])
            #Xmin, Xmax, Ymin, Ymax = x1, x2, y1, y2
            Xmin = int(x1 - (x2 - x1) / 2)
            Xmax = int(x2 + (x2 - x1) / 2)
            Ymin = int(y1 - (y2 - y1) / 2)
            Ymax = int(y2 + (y2 - y1) / 2)
            if Xmin < 0: Xmin = 0
            if Ymin < 0: Ymin = 0
            if Xmax > bin.shape[1]: Xmax = bin.shape[1]
            if Ymax > bin.shape[0]: Ymax = bin.shape[0]
            if cnt[i] > area_thresh:
                start_point = QPoint(x1,y1)
                end_point = QPoint(x2,y2)
                roi_img = self.opencvImgData[Ymin:Ymax, Xmin:Xmax, :]
                print('正在生成候选框')
                self.genBBoxFromPoint(start_point,end_point,roi_img)

    def adjustContrast(self):
        if isinstance(self.opencvImgData, np.ndarray):
            cur_val = self.adjust_brightness_scrollBar.value()
            # image processing
            hls = cv2.cvtColor(self.opencvImgData, cv2.COLOR_RGB2HLS)
            h, l, s = cv2.split(hls)
            l = l.astype(np.double)
            l *= 255 / cur_val
            l = np.where(l > 255, 255, l).astype(np.uint8)
            hls = cv2.merge([h, l, s])
            img = cv2.cvtColor(hls, cv2.COLOR_HLS2RGB)
            self.imageData = opencv2QImage(img)
            shapes = self.canvas.shapes
            self.canvas.loadPixmap(QPixmap.fromImage(self.imageData))
            self.canvas.shapes = shapes

    def predict3classes(self, img):
        img = np.asarray(img)
        if img.shape[2] != 3:
            img = img[:, :, 0:3]
        img = Image.fromarray(img)
        img_tensor = self.transform(img).unsqueeze(0).to(self.device)
        with torch.no_grad():
            out = self.model(img_tensor)
            label = F.softmax(out, dim=1).max(1)[1]
            #label_idx = out.max(1)[1]
            if label == 0:
                return 'particle'
            if label == 1:
                return 'bubble'
            if label == 2:
                return 'plankton'








        
    def keyReleaseEvent(self, event):
        if event.key() == Qt.Key_Control:
            self.canvas.setDrawingShapeToSquare(False)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Control:
            # Draw rectangle if Ctrl is pressed
            self.canvas.setDrawingShapeToSquare(True)

    ## Support Functions ##
    def set_format(self, save_format):
        if save_format == FORMAT_PASCALVOC:
            self.actions.save_format.setText(FORMAT_PASCALVOC)
            self.actions.save_format.setIcon(newIcon("format_voc"))
            self.labelFileFormat = LabelFileFormat.PASCAL_VOC
            LabelFile.suffix = XML_EXT

        elif save_format == FORMAT_YOLO:
            self.actions.save_format.setText(FORMAT_YOLO)
            self.actions.save_format.setIcon(newIcon("format_yolo"))
            self.labelFileFormat = LabelFileFormat.YOLO
            LabelFile.suffix = TXT_EXT

        elif save_format == FORMAT_CREATEML:
            self.actions.save_format.setText(FORMAT_CREATEML)
            self.actions.save_format.setIcon(newIcon("format_createml"))
            self.labelFileFormat = LabelFileFormat.CREATE_ML
            LabelFile.suffix = JSON_EXT

    def change_format(self):
        if self.labelFileFormat == LabelFileFormat.PASCAL_VOC:
            self.set_format(FORMAT_YOLO)
        elif self.labelFileFormat == LabelFileFormat.YOLO:
            self.set_format(FORMAT_CREATEML)
        elif self.labelFileFormat == LabelFileFormat.CREATE_ML:
            self.set_format(FORMAT_PASCALVOC)
        else:
            raise ValueError('Unknown label file format.')
        self.setDirty()

    def noShapes(self):
        return not self.itemsToShapes

    def toggleAdvancedMode(self, value=True):
        self._beginner = not value
        self.canvas.setEditing(True)
        self.populateModeActions()
        self.editButton.setVisible(not value)
        if value:
            self.actions.createMode.setEnabled(True)
            self.actions.editMode.setEnabled(False)
            self.dock.setFeatures(self.dock.features() | self.dockFeatures)
        else:
            self.dock.setFeatures(self.dock.features() ^ self.dockFeatures)

    def populateModeActions(self):
        if self.beginner():
            tool, menu = self.actions.beginner, self.actions.beginnerContext
        else:
            tool, menu = self.actions.advanced, self.actions.advancedContext
        self.tools.clear()
        addActions(self.tools, tool)
        self.canvas.menus[0].clear()
        addActions(self.canvas.menus[0], menu)
        self.menus.edit.clear()
        actions = (self.actions.create,) if self.beginner() \
            else (self.actions.createMode, self.actions.editMode)
        addActions(self.menus.edit, actions + self.actions.editMenu)

    def setBeginner(self):
        self.tools.clear()
        addActions(self.tools, self.actions.beginner)

    def setAdvanced(self):
        self.tools.clear()
        addActions(self.tools, self.actions.advanced)

    def setDirty(self):
        self.dirty = True
        self.actions.save.setEnabled(True)

    def setClean(self):
        self.dirty = False
        self.actions.save.setEnabled(False)
        self.actions.create.setEnabled(True)

    def toggleActions(self, value=True):
        """Enable/Disable widgets which depend on an opened image."""
        for z in self.actions.zoomActions:
            z.setEnabled(value)
        for action in self.actions.onLoadActive:
            action.setEnabled(value)

    def queueEvent(self, function):
        QTimer.singleShot(0, function)

    def status(self, message, delay=5000):
        self.statusBar().showMessage(message, delay)

    def resetState(self):
        self.itemsToShapes.clear()
        self.shapesToItems.clear()
        self.labelList.clear()
        self.filePath = None
        self.imageData = None
        self.labelFile = None
        self.opencvImgData = None
        self.canvas.resetState()
        self.labelCoordinates.clear()
        self.comboBox.cb.clear()

    def currentItem(self):
        items = self.labelList.selectedItems()
        if items:
            return items[0]
        return None

    def addRecentFile(self, filePath):
        if filePath in self.recentFiles:
            self.recentFiles.remove(filePath)
        elif len(self.recentFiles) >= self.maxRecent:
            self.recentFiles.pop()
        self.recentFiles.insert(0, filePath)

    def beginner(self):
        return self._beginner

    def advanced(self):
        return not self.beginner()

    def getAvailableScreencastViewer(self):
        osName = platform.system()

        if osName == 'Windows':
            return ['C:\\Program Files\\Internet Explorer\\iexplore.exe']
        elif osName == 'Linux':
            return ['xdg-open']
        elif osName == 'Darwin':
            return ['open']

    ## Callbacks ##
    def showTutorialDialog(self):
        subprocess.Popen(self.screencastViewer + [self.screencast])

    def showInfoDialog(self):
        from libs.__init__ import __version__
        msg = u'Name:{0} \nApp Version:{1} \n{2} '.format(__appname__, __version__, sys.version_info)
        QMessageBox.information(self, u'Information', msg)

    def createShape(self):  # 按下Create RectBox后即执行该函数
        assert self.beginner()
        self.canvas.setEditing(False)   # 设画布为CREATE模式
        self.actions.create.setEnabled(False)   # 按下按钮后，失能按钮

    def toggleDrawingSensitive(self, drawing=True):
        """In the middle of drawing, toggling between modes should be disabled."""
        self.actions.editMode.setEnabled(not drawing)   # 失能了editMode（不知道这是啥）
        if not drawing and self.beginner():
            # Cancel creation.
            print('Cancel creation.')
            self.canvas.setEditing(True)
            self.canvas.restoreCursor()
            self.actions.create.setEnabled(True)

    def toggleDrawMode(self, edit=True):
        self.canvas.setEditing(edit)
        self.actions.createMode.setEnabled(edit)
        self.actions.editMode.setEnabled(not edit)

    def setCreateMode(self):
        assert self.advanced()
        self.toggleDrawMode(False)

    def setEditMode(self):
        assert self.advanced()
        self.toggleDrawMode(True)
        self.labelSelectionChanged()

    def updateFileMenu(self):
        currFilePath = self.filePath

        def exists(filename):
            return os.path.exists(filename)

        menu = self.menus.recentFiles
        menu.clear()
        files = [f for f in self.recentFiles if f !=
                 currFilePath and exists(f)]
        for i, f in enumerate(files):
            icon = newIcon('labels')
            action = QAction(
                icon, '&%d %s' % (i + 1, QFileInfo(f).fileName()), self)
            action.triggered.connect(partial(self.loadRecent, f))
            menu.addAction(action)

    def popLabelListMenu(self, point):
        self.menus.labelList.exec_(self.labelList.mapToGlobal(point))

    def editLabel(self):
        if not self.canvas.editing():
            return
        item = self.currentItem()
        if not item:
            return
        text = self.labelDialog.popUp(item.text())
        if text is not None:
            item.setText(text)
            item.setBackground(generateColorByText(text))
            self.setDirty()
            self.updateComboBox()

    # Tzutalin 20160906 : Add file list and dock to move faster
    def fileitemDoubleClicked(self, item=None):
        currIndex = self.mImgList.index(ustr(item.text()))
        if currIndex < len(self.mImgList):
            filename = self.mImgList[currIndex]
            if filename:
                self.loadFile(filename)

    # Add chris
    def btnstate(self, item=None):
        """ Function to handle difficult examples
        Update on each object """
        if not self.canvas.editing():
            return

        item = self.currentItem()
        if not item:  # If not selected Item, take the first one
            item = self.labelList.item(self.labelList.count() - 1)

        difficult = self.diffcButton.isChecked()

        try:
            shape = self.itemsToShapes[item]
        except:
            pass
        # Checked and Update
        try:
            if difficult != shape.difficult:
                shape.difficult = difficult
                self.setDirty()
            else:  # User probably changed item visibility
                self.canvas.setShapeVisible(shape, item.checkState() == Qt.Checked)
        except:
            pass

    # React to canvas signals.
    def shapeSelectionChanged(self, selected=False):
        if self._noSelectionSlot:
            self._noSelectionSlot = False
        else:
            shape = self.canvas.selectedShape
            if shape:
                self.shapesToItems[shape].setSelected(True)
            else:
                self.labelList.clearSelection()
        self.actions.delete.setEnabled(selected)
        self.actions.copy.setEnabled(selected)
        self.actions.edit.setEnabled(selected)
        self.actions.shapeLineColor.setEnabled(selected)
        self.actions.shapeFillColor.setEnabled(selected)

    def addLabel(self, shape):
        shape.paintLabel = self.displayLabelOption.isChecked()
        item = HashableQListWidgetItem(shape.label)
        item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
        item.setCheckState(Qt.Checked)
        item.setBackground(generateColorByText(shape.label))
        self.itemsToShapes[item] = shape
        self.shapesToItems[shape] = item
        self.labelList.addItem(item)
        for action in self.actions.onShapesPresent:
            action.setEnabled(True)
        self.updateComboBox()

    def remLabel(self, shape):
        if shape is None:
            # print('rm empty label')
            return
        item = self.shapesToItems[shape]
        self.labelList.takeItem(self.labelList.row(item))
        del self.shapesToItems[shape]
        del self.itemsToShapes[item]
        self.updateComboBox()

    def loadLabels(self, shapes):
        s = []
        for label, points, line_color, fill_color, difficult in shapes:
            shape = Shape(label=label)
            for x, y in points:

                # Ensure the labels are within the bounds of the image. If not, fix them.
                x, y, snapped = self.canvas.snapPointToCanvas(x, y)
                if snapped:
                    self.setDirty()

                shape.addPoint(QPointF(x, y))
            shape.difficult = difficult
            shape.close()
            s.append(shape)

            if line_color:
                shape.line_color = QColor(*line_color)
            else:
                shape.line_color = generateColorByText(label)

            if fill_color:
                shape.fill_color = QColor(*fill_color)
            else:
                shape.fill_color = generateColorByText(label)

            self.addLabel(shape)
        self.updateComboBox()
        self.canvas.loadShapes(s)

    def updateComboBox(self):
        # Get the unique labels and add them to the Combobox.
        itemsTextList = [str(self.labelList.item(i).text()) for i in range(self.labelList.count())]

        uniqueTextList = list(set(itemsTextList))
        # Add a null row for showing all the labels
        uniqueTextList.append("")
        uniqueTextList.sort()

        self.comboBox.update_items(uniqueTextList)

    def saveLabels(self, annotationFilePath):
        annotationFilePath = ustr(annotationFilePath)
        if self.labelFile is None:
            self.labelFile = LabelFile()
            self.labelFile.verified = self.canvas.verified

        def format_shape(s):
            return dict(label=s.label,
                        line_color=s.line_color.getRgb(),
                        fill_color=s.fill_color.getRgb(),
                        points=[(p.x(), p.y()) for p in s.points],
                        # add chris
                        difficult=s.difficult)

        shapes = [format_shape(shape) for shape in self.canvas.shapes]
        # Can add differrent annotation formats here
        try:
            if self.labelFileFormat == LabelFileFormat.PASCAL_VOC:
                if annotationFilePath[-4:].lower() != ".xml":
                    annotationFilePath += XML_EXT
                self.labelFile.savePascalVocFormat(annotationFilePath, shapes, self.filePath, self.imageData,
                                                   self.lineColor.getRgb(), self.fillColor.getRgb())
            elif self.labelFileFormat == LabelFileFormat.YOLO:
                if annotationFilePath[-4:].lower() != ".txt":
                    annotationFilePath += TXT_EXT
                self.labelFile.saveYoloFormat(annotationFilePath, shapes, self.filePath, self.imageData, self.labelHist,
                                              self.lineColor.getRgb(), self.fillColor.getRgb())
            elif self.labelFileFormat == LabelFileFormat.CREATE_ML:
                if annotationFilePath[-5:].lower() != ".json":
                    annotationFilePath += JSON_EXT
                self.labelFile.saveCreateMLFormat(annotationFilePath, shapes, self.filePath, self.imageData,
                                                  self.labelHist, self.lineColor.getRgb(), self.fillColor.getRgb())
            else:
                self.labelFile.save(annotationFilePath, shapes, self.filePath, self.imageData,
                                    self.lineColor.getRgb(), self.fillColor.getRgb())
            print('Image:{0} -> Annotation:{1}'.format(self.filePath, annotationFilePath))
            return True
        except LabelFileError as e:
            self.errorMessage(u'Error saving label data', u'<b>%s</b>' % e)
            return False

    def copySelectedShape(self):
        self.addLabel(self.canvas.copySelectedShape())
        # fix copy and delete
        self.shapeSelectionChanged(True)

    def comboSelectionChanged(self, index):
        text = self.comboBox.cb.itemText(index)
        for i in range(self.labelList.count()):
            if text == "":
                self.labelList.item(i).setCheckState(2)
            elif text != self.labelList.item(i).text():
                self.labelList.item(i).setCheckState(0)
            else:
                self.labelList.item(i).setCheckState(2)

    def labelSelectionChanged(self):
        item = self.currentItem()
        if item and self.canvas.editing():
            self._noSelectionSlot = True
            self.canvas.selectShape(self.itemsToShapes[item])
            shape = self.itemsToShapes[item]
            # Add Chris
            self.diffcButton.setChecked(shape.difficult)

    def labelItemChanged(self, item):
        shape = self.itemsToShapes[item]
        label = item.text()
        if label != shape.label:
            shape.label = item.text()
            shape.line_color = generateColorByText(shape.label)
            self.setDirty()
        else:  # User probably changed item visibility
            self.canvas.setShapeVisible(shape, item.checkState() == Qt.Checked)

    # Callback functions:
    def newShape(self):         # TODO 【重点函数】弹出填写标注label的对话框
        """Pop-up and give focus to the label editor.

        position MUST be in global coordinates.
        """
        if not self.useDefaultLabelCheckbox.isChecked() or not self.defaultLabelTextLine.text():    # 如果use default label候选框没选或者内容没填，则手工输入类别
            if len(self.labelHist) > 0:
                self.labelDialog = LabelDialog(
                    parent=self, listItem=self.labelHist)

            # Sync single class mode from PR#106
            if self.singleClassMode.isChecked() and self.lastLabel:
                text = self.lastLabel
            else:
                text = self.labelDialog.popUp(text=self.prevLabelText)  # 返回值为选择的类别label名称
                self.lastLabel = text
        else:
            text = self.defaultLabelTextLine.text()     # 如果选中了use default label，则默认直接使用给定的标签

        # Add Chris
        self.diffcButton.setChecked(False)      # TODO 不知道difficult标签是干什么的
        if text is not None:
            self.prevLabelText = text
            generate_color = generateColorByText(text)  # 给标注为不同类别的bbox分配不同的颜色
            shape = self.canvas.setLastLabel(text, generate_color, generate_color)
            self.addLabel(shape)
            if self.beginner():  # Switch to edit mode.
                self.canvas.setEditing(True)
                self.actions.create.setEnabled(True)
            else:
                self.actions.editMode.setEnabled(True)
            self.setDirty()     # 看起来像是如果图像已经有过标注了，就把dirty设为true

            if text not in self.labelHist:
                self.labelHist.append(text)
        else:
            # self.canvas.undoLastLine()
            self.canvas.resetAllLines()

    def scrollRequest(self, delta, orientation):
        units = - delta / (8 * 15)
        bar = self.scrollBars[orientation]
        bar.setValue(bar.value() + bar.singleStep() * units)

    def setZoom(self, value):
        self.actions.fitWidth.setChecked(False)
        self.actions.fitWindow.setChecked(False)
        self.zoomMode = self.MANUAL_ZOOM
        self.zoomWidget.setValue(value)

    def addZoom(self, increment=10):
        self.setZoom(self.zoomWidget.value() + increment)

    def zoomRequest(self, delta):
        # get the current scrollbar positions
        # calculate the percentages ~ coordinates
        h_bar = self.scrollBars[Qt.Horizontal]
        v_bar = self.scrollBars[Qt.Vertical]

        # get the current maximum, to know the difference after zooming
        h_bar_max = h_bar.maximum()
        v_bar_max = v_bar.maximum()

        # get the cursor position and canvas size
        # calculate the desired movement from 0 to 1
        # where 0 = move left
        #       1 = move right
        # up and down analogous
        cursor = QCursor()
        pos = cursor.pos()
        relative_pos = QWidget.mapFromGlobal(self, pos)

        cursor_x = relative_pos.x()
        cursor_y = relative_pos.y()

        w = self.scrollArea.width()  # w,h是图像的绝对大小，不随滚动而变化
        h = self.scrollArea.height()

        # the scaling from 0 to 1 has some padding
        # you don't have to hit the very leftmost pixel for a maximum-left movement
        margin = 0.1
        move_x = (cursor_x - margin * w) / (w - 2 * margin * w)
        move_y = (cursor_y - margin * h) / (h - 2 * margin * h)

        # clamp the values from 0 to 1
        move_x = min(max(move_x, 0), 1)
        move_y = min(max(move_y, 0), 1)

        # zoom in
        units = delta / (8 * 15)
        scale = 10
        self.addZoom(scale * units)

        # get the difference in scrollbar values
        # this is how far we can move
        d_h_bar_max = h_bar.maximum() - h_bar_max
        d_v_bar_max = v_bar.maximum() - v_bar_max

        # get the new scrollbar values
        new_h_bar_value = h_bar.value() + move_x * d_h_bar_max
        new_v_bar_value = v_bar.value() + move_y * d_v_bar_max

        h_bar.setValue(new_h_bar_value)
        v_bar.setValue(new_v_bar_value)

    def setFitWindow(self, value=True):
        if value:
            self.actions.fitWidth.setChecked(False)
        self.zoomMode = self.FIT_WINDOW if value else self.MANUAL_ZOOM
        self.adjustScale()

    def setFitWidth(self, value=True):
        if value:
            self.actions.fitWindow.setChecked(False)
        self.zoomMode = self.FIT_WIDTH if value else self.MANUAL_ZOOM
        self.adjustScale()

    def togglePolygons(self, value):
        for item, shape in self.itemsToShapes.items():
            item.setCheckState(Qt.Checked if value else Qt.Unchecked)

    def loadFile(self, filePath=None):  # TODO【重要函数】加载图像。这个函数是加载单张图像的
        """Load the specified file, or the last opened file if None."""
        self.resetState()  # TODO【重要函数】对新数据恢复初始状态
        self.canvas.setEnabled(False)
        if filePath is None:
            filePath = self.settings.get(SETTING_FILENAME)

        # Make sure that filePath is a regular python string, rather than QString
        filePath = ustr(os.path.abspath(filePath))

        # Fix bug: An  index error after select a directory when open a new file.
        unicodeFilePath = ustr(filePath)
        unicodeFilePath = os.path.abspath(unicodeFilePath)
        # Tzutalin 20160906 : Add file list and dock to move faster
        # Highlight the file item
        if unicodeFilePath and self.fileListWidget.count() > 0:
            if unicodeFilePath in self.mImgList:
                index = self.mImgList.index(unicodeFilePath)
                fileWidgetItem = self.fileListWidget.item(index)
                fileWidgetItem.setSelected(True)
            else:
                self.fileListWidget.clear()
                self.mImgList.clear()

        if unicodeFilePath and os.path.exists(unicodeFilePath):
            if LabelFile.isLabelFile(unicodeFilePath):
                try:
                    self.labelFile = LabelFile(unicodeFilePath)
                except LabelFileError as e:
                    self.errorMessage(u'Error opening file',
                                      (u"<p><b>%s</b></p>"
                                       u"<p>Make sure <i>%s</i> is a valid label file.")
                                      % (e, unicodeFilePath))
                    self.status("Error reading %s" % unicodeFilePath)
                    return False
                self.imageData = self.labelFile.imageData
                self.lineColor = QColor(*self.labelFile.lineColor)
                self.fillColor = QColor(*self.labelFile.fillColor)
                self.canvas.verified = self.labelFile.verified
            else:
                # Load image:
                # read data first and store for saving into label file.
                self.opencvImgData = readByOpenCV(unicodeFilePath, None)
                #            self.imageData = read(unicodeFilePath, None)
                self.imageData = opencv2QImage(self.opencvImgData)
                self.labelFile = None
                self.canvas.verified = False

            if isinstance(self.imageData, QImage):
                image = self.imageData
            else:
                image = QImage.fromData(self.imageData)
            if image.isNull():  # TODO 这边还有点bug，如果不选择图像程序会闪退
                self.errorMessage(u'Error opening file',
                                  u"<p>Make sure <i>%s</i> is a valid image file." % unicodeFilePath)
                self.status("Error reading %s" % unicodeFilePath)
                return False
            self.status("Loaded %s" % os.path.basename(unicodeFilePath))
            self.image = image
            self.filePath = unicodeFilePath
            self.canvas.loadPixmap(QPixmap.fromImage(image))
            if self.labelFile:
                self.loadLabels(self.labelFile.shapes)
            self.setClean()
            self.canvas.setEnabled(True)  # 图像加载完毕后使能图像画框
            self.adjustScale(initial=True)
            self.paintCanvas()
            self.addRecentFile(self.filePath)
            self.toggleActions(True)
            self.showBoundingBoxFromAnnotationFile(filePath)

            self.setWindowTitle(__appname__ + ' ' + filePath)

            # Default : select last item if there is at least one item
            if self.labelList.count():
                self.labelList.setCurrentItem(self.labelList.item(self.labelList.count() - 1))
                self.labelList.item(self.labelList.count() - 1).setSelected(True)

            self.canvas.setFocus(True)
            return True
        return False
    def hasBoundingBoxFromAnnotationFile(self, filePath):
        if self.defaultSaveDir is not None:
            basename = os.path.basename(
                os.path.splitext(filePath)[0])
            path_split_mark = "\\" if platform.system() == 'Windows' else '/'
            filedir = filePath.split(basename)[0].split(path_split_mark)[-2:-1][0]

            xmlPath = os.path.join(self.defaultSaveDir, basename + XML_EXT)
            txtPath = os.path.join(self.defaultSaveDir, basename + TXT_EXT)
            jsonPath = os.path.join(self.defaultSaveDir, filedir + JSON_EXT)

            if os.path.isfile(xmlPath):
                return True
            elif os.path.isfile(txtPath):
                return True
            elif os.path.isfile(jsonPath):
                return True
            else:
                return False

    def showBoundingBoxFromAnnotationFile(self, filePath):
        if self.defaultSaveDir is not None:
            basename = os.path.basename(
                os.path.splitext(filePath)[0])
            path_split_mark = "\\" if platform.system() == 'Windows' else '/'
            filedir = filePath.split(basename)[0].split(path_split_mark)[-2:-1][0]

            xmlPath = os.path.join(self.defaultSaveDir, basename + XML_EXT)
            txtPath = os.path.join(self.defaultSaveDir, basename + TXT_EXT)
            jsonPath = os.path.join(self.defaultSaveDir, filedir + JSON_EXT)

            """Annotation file priority:
            PascalXML > YOLO
            """
            if os.path.isfile(xmlPath):
                self.loadPascalXMLByFilename(xmlPath)
            elif os.path.isfile(txtPath):
                self.loadYOLOTXTByFilename(txtPath)
            elif os.path.isfile(jsonPath):
                self.loadCreateMLJSONByFilename(jsonPath, filePath)

        else:
            xmlPath = os.path.splitext(filePath)[0] + XML_EXT
            txtPath = os.path.splitext(filePath)[0] + TXT_EXT
            if os.path.isfile(xmlPath):
                self.loadPascalXMLByFilename(xmlPath)
            elif os.path.isfile(txtPath):
                self.loadYOLOTXTByFilename(txtPath)

    def resizeEvent(self, event):
        if self.canvas and not self.image.isNull() \
                and self.zoomMode != self.MANUAL_ZOOM:
            self.adjustScale()
        super(MainWindow, self).resizeEvent(event)

    def paintCanvas(self):
        assert not self.image.isNull(), "cannot paint null image"
        self.canvas.scale = 0.01 * self.zoomWidget.value()
        self.canvas.adjustSize()
        self.canvas.update()

    def adjustScale(self, initial=False):
        value = self.scalers[self.FIT_WINDOW if initial else self.zoomMode]()
        self.zoomWidget.setValue(int(100 * value))

    def scaleFitWindow(self):
        """Figure out the size of the pixmap in order to fit the main widget."""
        e = 2.0  # So that no scrollbars are generated.
        w1 = self.centralWidget().width() - e
        h1 = self.centralWidget().height() - e
        a1 = w1 / h1
        # Calculate a new scale value based on the pixmap's aspect ratio.
        w2 = self.canvas.pixmap.width() - 0.0
        h2 = self.canvas.pixmap.height() - 0.0
        a2 = w2 / h2
        return w1 / w2 if a2 >= a1 else h1 / h2

    def scaleFitWidth(self):
        # The epsilon does not seem to work too well here.
        w = self.centralWidget().width() - 2.0
        return w / self.canvas.pixmap.width()

    def closeEvent(self, event):
        if not self.mayContinue():
            event.ignore()
        settings = self.settings
        # If it loads images from dir, don't load it at the begining
        if self.dirname is None:
            settings[SETTING_FILENAME] = self.filePath if self.filePath else ''
        else:
            settings[SETTING_FILENAME] = ''

        settings[SETTING_WIN_SIZE] = self.size()
        settings[SETTING_WIN_POSE] = self.pos()
        settings[SETTING_WIN_STATE] = self.saveState()
        settings[SETTING_LINE_COLOR] = self.lineColor
        settings[SETTING_FILL_COLOR] = self.fillColor
        settings[SETTING_RECENT_FILES] = self.recentFiles
        settings[SETTING_ADVANCE_MODE] = not self._beginner
        if self.defaultSaveDir and os.path.exists(self.defaultSaveDir):
            settings[SETTING_SAVE_DIR] = ustr(self.defaultSaveDir)
        else:
            settings[SETTING_SAVE_DIR] = ''

        if self.lastOpenDir and os.path.exists(self.lastOpenDir):
            settings[SETTING_LAST_OPEN_DIR] = self.lastOpenDir
        else:
            settings[SETTING_LAST_OPEN_DIR] = ''

        settings[SETTING_AUTO_SAVE] = self.autoSaving.isChecked()
        settings[SETTING_SINGLE_CLASS] = self.singleClassMode.isChecked()
        settings[SETTING_PAINT_LABEL] = self.displayLabelOption.isChecked()
        settings[SETTING_DRAW_SQUARE] = self.drawSquaresOption.isChecked()
        settings[SETTING_LABEL_FILE_FORMAT] = self.labelFileFormat
        settings.save()

    def loadRecent(self, filename):
        if self.mayContinue():
            self.loadFile(filename)

    def scanAllImages(self, folderPath):
        extensions = ['.%s' % fmt.data().decode("ascii").lower() for fmt in QImageReader.supportedImageFormats()]
        images = []

        for root, dirs, files in os.walk(folderPath):
            for file in files:
                if file.lower().endswith(tuple(extensions)):
                    relativePath = os.path.join(root, file)
                    path = ustr(os.path.abspath(relativePath))  # 利用os.path.abspath()可以将混杂了/和\\符号的路径根据程序运行的平台同一转换成一种表示
                    images.append(path)
        natural_sort(images, key=lambda x: x.lower())
        return images

    def changeSavedirDialog(self, _value=False):
        if self.defaultSaveDir is not None:
            path = ustr(self.defaultSaveDir)
        else:
            path = '.'

        dirpath = ustr(QFileDialog.getExistingDirectory(self,
                                                        '%s - Save annotations to the directory' % __appname__, path,
                                                        QFileDialog.ShowDirsOnly
                                                        | QFileDialog.DontResolveSymlinks))

        if dirpath is not None and len(dirpath) > 1:
            self.defaultSaveDir = dirpath

        self.statusBar().showMessage('%s . Annotation will be saved to %s' %
                                     ('Change saved folder', self.defaultSaveDir))
        self.statusBar().show()

    def openAnnotationDialog(self, _value=False):
        if self.filePath is None:
            self.statusBar().showMessage('Please select image first')
            self.statusBar().show()
            return

        path = os.path.dirname(ustr(self.filePath)) \
            if self.filePath else '.'
        if self.labelFileFormat == LabelFileFormat.PASCAL_VOC:
            filters = "Open Annotation XML file (%s)" % ' '.join(['*.xml'])
            filename = ustr(QFileDialog.getOpenFileName(self, '%s - Choose a xml file' % __appname__, path, filters))
            if filename:
                if isinstance(filename, (tuple, list)):
                    filename = filename[0]
            self.loadPascalXMLByFilename(filename)

    def openDirDialog(self, _value=False, dirpath=None, silent=False):
        if not self.mayContinue():
            return

        defaultOpenDirPath = dirpath if dirpath else '.'
        if self.lastOpenDir and os.path.exists(self.lastOpenDir):
            defaultOpenDirPath = self.lastOpenDir
        else:
            defaultOpenDirPath = os.path.dirname(self.filePath) if self.filePath else '.'
        if silent != True:
            targetDirPath = ustr(QFileDialog.getExistingDirectory(self,
                                                                  '%s - Open Directory' % __appname__,
                                                                  defaultOpenDirPath,
                                                                  QFileDialog.ShowDirsOnly | QFileDialog.DontResolveSymlinks))
        else:
            targetDirPath = ustr(defaultOpenDirPath)
        self.lastOpenDir = targetDirPath
        self.importDirImages(targetDirPath)

    def importDirImages(self, dirpath):
        if not self.mayContinue() or not dirpath:
            return

        self.lastOpenDir = dirpath
        self.dirname = dirpath
        self.filePath = None
        self.fileListWidget.clear() # 清空fileList
        self.mImgList = self.scanAllImages(dirpath)     # 获得open的dir下的所有具有设定后缀名的图像文件，包括子文件夹中的
        self.openNextImg()
        for imgPath in self.mImgList:
            item = QListWidgetItem(imgPath)
            self.fileListWidget.addItem(item)

    def verifyImg(self, _value=False):
        # Proceding next image without dialog if having any label
        if self.filePath is not None:
            try:
                self.labelFile.toggleVerify()
            except AttributeError:
                # If the labelling file does not exist yet, create if and
                # re-save it with the verified attribute.
                self.saveFile()
                if self.labelFile != None:
                    self.labelFile.toggleVerify()
                else:
                    return

            self.canvas.verified = self.labelFile.verified
            self.paintCanvas()
            self.saveFile()

    def openPrevImg(self, _value=False):
        # Proceding prev image without dialog if having any label
        if self.autoSaving.isChecked():
            if self.defaultSaveDir is not None:
                if self.dirty is True:
                    self.saveFile()
            else:
                self.changeSavedirDialog()
                return

        if not self.mayContinue():
            return

        if len(self.mImgList) <= 0:
            return

        if self.filePath is None:
            return

        currIndex = self.mImgList.index(self.filePath)
        if currIndex - 1 >= 0:
            filename = self.mImgList[currIndex - 1]
            if filename:
                self.loadFile(filename)

    def openNextImg(self, _value=False):
        # Proceding prev image without dialog if having any label
        if self.autoSaving.isChecked():
            if self.defaultSaveDir is not None:
                if self.dirty is True:
                    self.saveFile()
            else:
                self.changeSavedirDialog()
                return

        if not self.mayContinue():
            return

        if len(self.mImgList) <= 0:
            return

        filename = None
        if self.filePath is None:
            filename = self.mImgList[0]
        else:
            currIndex = self.mImgList.index(self.filePath)
            if currIndex + 1 < len(self.mImgList):
                filename = self.mImgList[currIndex + 1]

        if filename:
            self.loadFile(filename)

    def openFile(self, _value=False):
        if not self.mayContinue():  # 是否可以继续的判断，里面包含弹出是否保存之前标注的对话框、当前的图像状态
            return
        path = os.path.dirname(ustr(self.filePath)) if self.filePath else '.'
        formats = ['*.%s' % fmt.data().decode("ascii").lower() for fmt in QImageReader.supportedImageFormats()]
        filters = "Image & Label files (%s)" % ' '.join(formats + ['*%s' % LabelFile.suffix])
        filename = QFileDialog.getOpenFileName(self, '%s - Choose Image or Label file' % __appname__, path, filters)
        if filename != ('',''):
            if isinstance(filename, (tuple, list)):
                filename = filename[0]
            self.loadFile(filename)

    def saveFile(self, _value=False):
        if self.defaultSaveDir is not None and len(ustr(self.defaultSaveDir)):
            if self.filePath:
                imgFileName = os.path.basename(self.filePath)
                savedFileName = os.path.splitext(imgFileName)[0]
                savedPath = os.path.join(ustr(self.defaultSaveDir), savedFileName)
                self._saveFile(savedPath)
        else:
            imgFileDir = os.path.dirname(self.filePath)
            imgFileName = os.path.basename(self.filePath)
            savedFileName = os.path.splitext(imgFileName)[0]
            savedPath = os.path.join(imgFileDir, savedFileName)
            self._saveFile(savedPath if self.labelFile
                           else self.saveFileDialog(removeExt=False))

    def saveFileAs(self, _value=False):
        assert not self.image.isNull(), "cannot save empty image"
        self._saveFile(self.saveFileDialog())

    def saveFileDialog(self, removeExt=True):
        caption = '%s - Choose File' % __appname__
        filters = 'File (*%s)' % LabelFile.suffix
        openDialogPath = self.currentPath()
        dlg = QFileDialog(self, caption, openDialogPath, filters)
        dlg.setDefaultSuffix(LabelFile.suffix[1:])
        dlg.setAcceptMode(QFileDialog.AcceptSave)
        filenameWithoutExtension = os.path.splitext(self.filePath)[0]
        dlg.selectFile(filenameWithoutExtension)
        dlg.setOption(QFileDialog.DontUseNativeDialog, False)
        if dlg.exec_():
            fullFilePath = ustr(dlg.selectedFiles()[0])
            if removeExt:
                return os.path.splitext(fullFilePath)[0]  # Return file path without the extension.
            else:
                return fullFilePath
        return ''

    def _saveFile(self, annotationFilePath):
        if annotationFilePath and self.saveLabels(annotationFilePath):
            self.setClean()
            self.statusBar().showMessage('Saved to  %s' % annotationFilePath)
            self.statusBar().show()

    def closeFile(self, _value=False):
        if not self.mayContinue():
            return
        self.resetState()
        self.setClean()
        self.toggleActions(False)
        self.canvas.setEnabled(False)
        self.actions.saveAs.setEnabled(False)

    def deleteImg(self):
        deletePath = self.filePath
        if deletePath is not None:
            self.openNextImg()
            if os.path.exists(deletePath):
                os.remove(deletePath)
            self.importDirImages(self.lastOpenDir)

    def resetAll(self):
        self.settings.reset()
        self.close()
        proc = QProcess()
        proc.startDetached(os.path.abspath(__file__))

    def mayContinue(self):
        if not self.dirty:
            return True
        else:
            discardChanges = self.discardChangesDialog()
            if discardChanges == QMessageBox.No:
                return True
            elif discardChanges == QMessageBox.Yes:
                self.saveFile()
                return True
            else:
                return False

    def discardChangesDialog(self):
        yes, no, cancel = QMessageBox.Yes, QMessageBox.No, QMessageBox.Cancel
        msg = u'You have unsaved changes, would you like to save them and proceed?\nClick "No" to undo all changes.'
        return QMessageBox.warning(self, u'Attention', msg, yes | no | cancel)

    def errorMessage(self, title, message):
        return QMessageBox.critical(self, title,
                                    '<p><b>%s</b></p>%s' % (title, message))

    def currentPath(self):
        return os.path.dirname(self.filePath) if self.filePath else '.'

    def chooseColor1(self):
        color = self.colorDialog.getColor(self.lineColor, u'Choose line color',
                                          default=DEFAULT_LINE_COLOR)
        if color:
            self.lineColor = color
            Shape.line_color = color
            self.canvas.setDrawingColor(color)
            self.canvas.update()
            self.setDirty()

    def deleteSelectedShape(self):
        self.remLabel(self.canvas.deleteSelected())
        self.setDirty()
        if self.noShapes():
            for action in self.actions.onShapesPresent:
                action.setEnabled(False)

    def chshapeLineColor(self):
        color = self.colorDialog.getColor(self.lineColor, u'Choose line color',
                                          default=DEFAULT_LINE_COLOR)
        if color:
            self.canvas.selectedShape.line_color = color
            self.canvas.update()
            self.setDirty()

    def chshapeFillColor(self):
        color = self.colorDialog.getColor(self.fillColor, u'Choose fill color',
                                          default=DEFAULT_FILL_COLOR)
        if color:
            self.canvas.selectedShape.fill_color = color
            self.canvas.update()
            self.setDirty()

    def copyShape(self):
        self.canvas.endMove(copy=True)
        self.addLabel(self.canvas.selectedShape)
        self.setDirty()

    def moveShape(self):
        self.canvas.endMove(copy=False)
        self.setDirty()

    def loadPredefinedClasses(self, predefClassesFile):
        if os.path.exists(predefClassesFile) is True:
            with codecs.open(predefClassesFile, 'r', 'utf8') as f:
                for line in f:
                    line = line.strip()
                    if self.labelHist is None:
                        self.labelHist = [line]
                    else:
                        self.labelHist.append(line)

    def loadPascalXMLByFilename(self, xmlPath):
        if self.filePath is None:
            return
        if os.path.isfile(xmlPath) is False:
            return

        self.set_format(FORMAT_PASCALVOC)

        tVocParseReader = PascalVocReader(xmlPath)
        shapes = tVocParseReader.getShapes()
        self.loadLabels(shapes)
        self.canvas.verified = tVocParseReader.verified

    def loadYOLOTXTByFilename(self, txtPath):
        if self.filePath is None:
            return
        if os.path.isfile(txtPath) is False:
            return

        self.set_format(FORMAT_YOLO)
        tYoloParseReader = YoloReader(txtPath, self.image)
        shapes = tYoloParseReader.getShapes()
        print(shapes)
        self.loadLabels(shapes)
        self.canvas.verified = tYoloParseReader.verified

    def loadCreateMLJSONByFilename(self, jsonPath, filePath):
        if self.filePath is None:
            return
        if os.path.isfile(jsonPath) is False:
            return

        self.set_format(FORMAT_CREATEML)

        crmlParseReader = CreateMLReader(jsonPath, filePath)
        shapes = crmlParseReader.get_shapes()
        self.loadLabels(shapes)
        self.canvas.verified = crmlParseReader.verified

    def copyPreviousBoundingBoxes(self):
        currIndex = self.mImgList.index(self.filePath)
        if currIndex - 1 >= 0:
            prevFilePath = self.mImgList[currIndex - 1]
            self.showBoundingBoxFromAnnotationFile(prevFilePath)
            self.saveFile()

    def togglePaintLabelsOption(self):
        for shape in self.canvas.shapes:
            shape.paintLabel = self.displayLabelOption.isChecked()

    def toogleDrawSquare(self):
        self.canvas.setDrawingShapeToSquare(self.drawSquaresOption.isChecked())


def inverted(color):
    return QColor(*[255 - v for v in color.getRgb()])


def readByOpenCV(filename, default=None):
    try:
        img = cv2.imdecode(np.fromfile(filename, dtype=np.uint8), -1)
        img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        return img
    except:
        return default


def opencv2QImage(img):
    cv_rows, cv_cols, cv_channels = img.shape
    bytesPerLine = cv_cols * cv_channels
    QImg = QImage(img.data, cv_cols, cv_rows, bytesPerLine, QImage.Format_RGB888)
    return QImg


def read(filename, default=None):
    try:
        reader = QImageReader(filename)
        reader.setAutoTransform(True)
        return reader.read()
    except:
        return default


def get_main_app(argv=[]):
    """
    Standard boilerplate Qt application code.
    Do everything but app.exec_() -- so that we can test the application in one thread
    """
    app = QApplication(argv)
    app.setApplicationName(__appname__)
    app.setWindowIcon(newIcon("app"))
    # Tzutalin 201705+: Accept extra agruments to change predefined class file
    argparser = argparse.ArgumentParser()
    argparser.add_argument("image_dir", nargs="?")
    argparser.add_argument("predefined_classes_file",
                           default=os.path.join(os.path.dirname(__file__), "data", "predefined_classes.txt"),
                           nargs="?")
    argparser.add_argument("save_dir", nargs="?")
    args = argparser.parse_args(argv[1:])
    # Usage : labelDarkFieldImg.py image predefClassFile saveDir
    win = MainWindow(args.image_dir,
                     args.predefined_classes_file,
                     args.save_dir)
    win.show()
    return app, win


def main():
    '''construct main app and run it'''
    app, _win = get_main_app(sys.argv)
    return app.exec_()


if __name__ == '__main__':
    sys.exit(main())
