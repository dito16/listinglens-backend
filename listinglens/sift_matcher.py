"""Independent SIFT/RANSAC evidence for AI-retouched photos.

Experimental conservative threshold policy R320-PROVISIONAL: this is NOT a
calibrated probability and should be validated on different apartments before
making general accuracy claims.
"""
from __future__ import annotations
from dataclasses import dataclass
import cv2
import numpy as np

# Conservative operational gate, not a statistically calibrated classifier.
RULE_VERSION = 'SIFT_GEOMETRY_R320_PROVISIONAL_1'
MIN_GOOD = 38
MIN_INLIERS = 30
MIN_INLIER_RATIO = 0.52
MIN_BOX_COVERAGE = 0.055
MAX_MEDIAN_REPROJ = 3.3
MIN_OCCUPIED_CELLS = 4


@dataclass(frozen=True)
class Features:
    keypoints: np.ndarray  # Nx2 coordinates
    descriptors: np.ndarray | None
    width: int
    height: int


def extract_features(photo):
    """JPEG grayscale representation from fingerprint, no network calls."""
    packed=getattr(photo, 'sift_jpeg', None)
    if not packed:
        return None
    frame=cv2.imdecode(np.frombuffer(packed,dtype=np.uint8),cv2.IMREAD_GRAYSCALE)
    if frame is None or frame.size==0:
        return None
    enhanced=cv2.createCLAHE(clipLimit=2.0,tileGridSize=(8,8)).apply(frame)
    sift=cv2.SIFT_create(nfeatures=600,contrastThreshold=0.025)
    keypoints, descriptors=sift.detectAndCompute(enhanced,None)
    coords=np.asarray([kp.pt for kp in keypoints],dtype=np.float32).reshape((-1,2))
    return Features(coords,descriptors,frame.shape[1],frame.shape[0])


def _bbox_coverage(points, w, h):
    if len(points)<4:return 0.0
    lo=points.min(axis=0);hi=points.max(axis=0)
    return float(max(0,(hi[0]-lo[0])*(hi[1]-lo[1]))/max(1,w*h))


def _occupied_cells(points,w,h):
    if len(points)==0:return 0
    ix=np.clip((points[:,0]/max(1,w)*4).astype(int),0,3)
    iy=np.clip((points[:,1]/max(1,h)*4).astype(int),0,3)
    return len(set((int(x),int(y)) for x,y in zip(ix,iy)))


def compare_features(a:Features|None,b:Features|None)->dict:
    proof=dict(method='SIFT_RANSAC',rule=RULE_VERSION,match=False,
        sift_good_matches=0,sift_ransac_inliers=0,sift_inlier_ratio=0.0,
        sift_reprojection_median_px=None,sift_coverage_source=0.0,
        sift_coverage_candidate=0.0,sift_geometry_valid=False,
        sift_occupied_cells_source=0,sift_occupied_cells_candidate=0,
        sift_keypoints_a=0,sift_keypoints_b=0)
    if a is None or b is None or a.descriptors is None or b.descriptors is None:
        proof['reason']='NO_SIFT_DESCRIPTORS';return proof
    proof['sift_keypoints_a']=len(a.keypoints);proof['sift_keypoints_b']=len(b.keypoints)
    if len(a.descriptors)<MIN_GOOD or len(b.descriptors)<MIN_GOOD:
        proof['reason']='TOO_FEW_SIFT_KEYPOINTS';return proof
    matcher=cv2.BFMatcher(cv2.NORM_L2)
    matches=matcher.knnMatch(a.descriptors,b.descriptors,k=2)
    good=[one for pair in matches if len(pair)==2 for one,two in [pair] if one.distance<0.75*two.distance]
    proof['sift_good_matches']=len(good)
    if len(good)<MIN_GOOD:
        proof['reason']='TOO_FEW_GOOD_MATCHES';return proof
    src=np.float32([a.keypoints[m.queryIdx] for m in good]).reshape(-1,1,2)
    dst=np.float32([b.keypoints[m.trainIdx] for m in good]).reshape(-1,1,2)
    H,mask=cv2.findHomography(src,dst,cv2.RANSAC,4.5,maxIters=1000,confidence=0.995)
    if H is None or mask is None or not np.all(np.isfinite(H)):
        proof['reason']='NO_VALID_HOMOGRAPHY';return proof
    m=mask.ravel().astype(bool);count=int(m.sum());ratio=count/max(1,len(good))
    proof['sift_ransac_inliers']=count
    proof['sift_inlier_ratio']=round(ratio,4)
    if count<4:
        proof['reason']='INSUFFICIENT_GEOMETRIC_INLIERS';return proof
    p_src=src.reshape(-1,2)[m];p_dst=dst.reshape(-1,2)[m]
    proof['sift_coverage_source']=round(_bbox_coverage(p_src,a.width,a.height),4)
    proof['sift_coverage_candidate']=round(_bbox_coverage(p_dst,b.width,b.height),4)
    proof['sift_occupied_cells_source']=_occupied_cells(p_src,a.width,a.height)
    proof['sift_occupied_cells_candidate']=_occupied_cells(p_dst,b.width,b.height)
    proj=cv2.perspectiveTransform(p_src.reshape(-1,1,2),H).reshape(-1,2)
    median=float(np.median(np.linalg.norm(proj-p_dst,axis=1)))
    proof['sift_reprojection_median_px']=round(median,3)
    corners=np.float32([[0,0],[a.width,0],[a.width,a.height],[0,a.height]]).reshape(-1,1,2)
    moved=cv2.perspectiveTransform(corners,H).reshape(-1,2)
    base=a.width*a.height
    area=abs(float(cv2.contourArea(moved)))
    # Reject degenerate, mirrored and wildly warped homographies.
    u=moved[1]-moved[0];v=moved[2]-moved[1]
    cross=float(u[0]*v[1]-u[1]*v[0])
    sensible=bool(np.all(np.isfinite(moved)) and 0.22<area/max(1,base)<4.5 and
        cross>0 and cv2.isContourConvex(moved.astype(np.float32)) and
        abs(float(np.linalg.det(H)))>1e-7)
    proof['sift_geometry_valid']=sensible
    proof['match']=bool(count>=MIN_INLIERS and len(good)>=MIN_GOOD and
        ratio>=MIN_INLIER_RATIO and proof['sift_coverage_source']>=MIN_BOX_COVERAGE and
        proof['sift_coverage_candidate']>=MIN_BOX_COVERAGE and
        proof['sift_occupied_cells_source']>=MIN_OCCUPIED_CELLS and
        proof['sift_occupied_cells_candidate']>=MIN_OCCUPIED_CELLS and
        median<=MAX_MEDIAN_REPROJ and sensible)
    proof['reason']='GEOMETRY_VERIFIED' if proof['match'] else 'GEOMETRY_BELOW_CONSERVATIVE_GATE'
    return proof
